/**
 * @license
 * Copyright 2025 Qwen
 * SPDX-License-Identifier: Apache-2.0
 */

import { parse } from 'shell-quote';
import {
  detectCommandSubstitution,
  splitCommands,
  stripShellWrapper,
} from './shell-utils.js';

const READ_ONLY_ROOT_COMMANDS = new Set([
  'awk',
  'basename',
  'cat',
  'cd',
  'column',
  'cut',
  'df',
  'dirname',
  'du',
  'echo',
  'env',
  'find',
  'git',
  'grep',
  'head',
  'less',
  'ls',
  'more',
  'printenv',
  'printf',
  'ps',
  'pwd',
  'rg',
  'ripgrep',
  'sed',
  'sort',
  'stat',
  'tail',
  'tree',
  'uniq',
  'wc',
  'which',
  'where',
  'whoami',
]);

const BLOCKED_FIND_FLAGS = new Set([
  '-delete',
  '-exec',
  '-execdir',
  '-ok',
  '-okdir',
]);

const BLOCKED_FIND_PREFIXES = ['-fprint', '-fprintf'];

const READ_ONLY_GIT_SUBCOMMANDS = new Set([
  'blame',
  'branch',
  'cat-file',
  'diff',
  'grep',
  'log',
  'ls-files',
  'remote',
  'rev-parse',
  'show',
  'status',
  'describe',
]);

const BLOCKED_GIT_REMOTE_ACTIONS = new Set([
  'add',
  'remove',
  'rename',
  'set-url',
  'prune',
  'update',
]);

const BLOCKED_GIT_BRANCH_FLAGS = new Set([
  '-d',
  '-D',
  '--delete',
  '--move',
  '-m',
  '-M',
  '-c',
  '-C',
  '-u',
  '--copy',
]);

const BLOCKED_GIT_BRANCH_PREFIXES = ['--set-upstream-to', '--unset-upstream'];

// `--output` on otherwise read-only git subcommands writes a file.
const BLOCKED_GIT_OUTPUT_FLAGS = ['--output'];

const BLOCKED_SED_PREFIXES = ['-i'];

// `env -S` / `--split-string` embed the command to run inside the option
// value; GNU env still executes it, so every spelling is rejected.
const BLOCKED_ENV_SPLIT_PREFIXES = ['-S', '--split-string'];
// `env -u NAME` / `--unset NAME` only drop a var from the printed env.
const ENV_VALUE_FLAGS = new Set(['-u', '--unset']);

// uniq options whose value may follow as a separate token.
const UNIQ_VALUE_FLAGS = new Set([
  '-f',
  '-s',
  '-w',
  '--skip-fields',
  '--skip-chars',
  '--check-chars',
]);

// rg flags that execute an external program.
const BLOCKED_RG_FLAGS = ['--pre', '--hostname-bin'];

// AWK side-effect patterns that can execute commands or write files
const AWK_SIDE_EFFECT_PATTERNS = [
  /system\s*\(/, // system() function calls
  /print\s+[^>|]*>\s*"[^"]*"/, // print > "file"
  /printf\s+[^>|]*>\s*"[^"]*"/, // printf > "file"
  /print\s+[^>|]*>>\s*"[^"]*"/, // print >> "file"
  /printf\s+[^>|]*>>\s*"[^"]*"/, // printf >> "file"
  /print\s+[^|]*\|\s*"[^"]*"/, // print | "command"
  /printf\s+[^|]*\|\s*"[^"]*"/, // printf | "command"
  /getline\s*<\s*"[^"]*"/, // getline < "command"
  /"[^"]*"\s*\|\s*getline/, // "command" | getline
  /close\s*\(/, // close() can trigger command execution
];

// SED side-effect patterns
const SED_SIDE_EFFECT_PATTERNS = [
  /[^\\]e\s/, // e command (execute)
  /^e\s/, // e command at start
  /[^\\]w\s/, // w command (write)
  /^w\s/, // w command at start
  /[^\\]r\s/, // r command (read file)
  /^r\s/, // r command at start
];

const ENV_ASSIGNMENT_REGEX = /^[A-Za-z_][A-Za-z0-9_]*=/;

function containsWriteRedirection(command: string): boolean {
  let inSingleQuotes = false;
  let inDoubleQuotes = false;
  let escapeNext = false;

  for (const char of command) {
    if (escapeNext) {
      escapeNext = false;
      continue;
    }

    if (char === '\\' && !inSingleQuotes) {
      escapeNext = true;
      continue;
    }

    if (char === "'" && !inDoubleQuotes) {
      inSingleQuotes = !inSingleQuotes;
      continue;
    }

    if (char === '"' && !inSingleQuotes) {
      inDoubleQuotes = !inDoubleQuotes;
      continue;
    }

    if (!inSingleQuotes && !inDoubleQuotes && char === '>') {
      return true;
    }
  }

  return false;
}

function normalizeTokens(segment: string): string[] {
  const parsed = parse(segment);
  const tokens: string[] = [];
  for (const token of parsed) {
    if (typeof token === 'string') {
      tokens.push(token);
    }
  }
  return tokens;
}

function skipEnvironmentAssignments(tokens: string[]): {
  root?: string;
  args: string[];
} {
  let index = 0;
  while (index < tokens.length && ENV_ASSIGNMENT_REGEX.test(tokens[index]!)) {
    index++;
  }

  if (index >= tokens.length) {
    return { args: [] };
  }

  return {
    root: tokens[index],
    args: tokens.slice(index + 1),
  };
}

function evaluateFindCommand(tokens: string[]): boolean {
  const [, ...rest] = tokens;
  for (const token of rest) {
    const lower = token.toLowerCase();
    if (BLOCKED_FIND_FLAGS.has(lower)) {
      return false;
    }
    if (BLOCKED_FIND_PREFIXES.some((prefix) => lower.startsWith(prefix))) {
      return false;
    }
  }
  return true;
}

function evaluateSedCommand(tokens: string[]): boolean {
  const [, ...rest] = tokens;
  for (const token of rest) {
    if (
      BLOCKED_SED_PREFIXES.some((prefix) => token.startsWith(prefix)) ||
      token.startsWith('--in-place')
    ) {
      return false;
    }
  }

  // Check for side-effect patterns in sed script
  const scriptContent = rest.join(' ');
  for (const pattern of SED_SIDE_EFFECT_PATTERNS) {
    if (pattern.test(scriptContent)) {
      return false;
    }
  }

  return true;
}

function evaluateAwkCommand(tokens: string[]): boolean {
  const [, ...rest] = tokens;

  // Join all arguments to check for awk script content
  const scriptContent = rest.join(' ');

  // Check for dangerous side-effect patterns
  for (const pattern of AWK_SIDE_EFFECT_PATTERNS) {
    if (pattern.test(scriptContent)) {
      return false;
    }
  }

  return true;
}

function evaluateGitRemoteArgs(args: string[]): boolean {
  for (const arg of args) {
    if (BLOCKED_GIT_REMOTE_ACTIONS.has(arg.toLowerCase())) {
      return false;
    }
  }
  return true;
}

function evaluateGitBranchArgs(args: string[]): boolean {
  for (const arg of args) {
    if (BLOCKED_GIT_BRANCH_FLAGS.has(arg)) {
      return false;
    }
    if (BLOCKED_GIT_BRANCH_PREFIXES.some((prefix) => arg.startsWith(prefix))) {
      return false;
    }
    // `-u` sets upstream; the exact token is in the flags set, but the
    // attached-value spelling `-uorigin/main` and clusters beginning with
    // it are equivalent and must also be rejected.
    if (/^-u/.test(arg)) {
      return false;
    }
  }
  return true;
}

function hasGitOutputFlag(args: string[]): boolean {
  return args.some(
    (arg) =>
      BLOCKED_GIT_OUTPUT_FLAGS.includes(arg) ||
      BLOCKED_GIT_OUTPUT_FLAGS.some((flag) => arg.startsWith(`${flag}=`)),
  );
}

function evaluateEnvCommand(args: string[]): boolean {
  let sawDoubleDash = false;
  for (let index = 0; index < args.length; index++) {
    const token = args[index]!;
    if (sawDoubleDash) {
      // Everything after `--` is the command env executes.
      return false;
    }
    if (token === '--') {
      sawDoubleDash = true;
      continue;
    }
    // `env -S"touch marker"` / `env --split-string="touch marker"` carry
    // the whole command inside a token that starts with `-`; the generic
    // option skip below would wave it through, so reject every spelling.
    if (BLOCKED_ENV_SPLIT_PREFIXES.some((prefix) => token.startsWith(prefix))) {
      return false;
    }
    // `env VAR=value` still only prints the environment.
    if (ENV_ASSIGNMENT_REGEX.test(token)) {
      continue;
    }
    // `-u NAME` / `--unset NAME` drop a var and print the env; consume the
    // operand so NAME is not mistaken for a command to run.
    if (ENV_VALUE_FLAGS.has(token)) {
      index++;
      continue;
    }
    if (token.startsWith('-')) {
      continue;
    }
    // Anything else is a command operand: env executes it.
    return false;
  }
  return true;
}

function evaluateSortCommand(args: string[]): boolean {
  for (const token of args) {
    if (token === '--') {
      // Remaining tokens are input files; sort never writes them.
      break;
    }
    // `--output FILE` and `--output=FILE` both write.
    if (token === '--output' || token.startsWith('--output=')) {
      return false;
    }
    // Short `-o` and any cluster containing it (`-no`, `-ro`, `-oFILE`)
    // take the following token (or the attached tail) as the output file.
    if (/^-(?!-)[^-]*o/.test(token)) {
      return false;
    }
  }
  return true;
}

function evaluateUniqCommand(args: string[]): boolean {
  let operands = 0;
  let sawDoubleDash = false;
  for (let index = 0; index < args.length; index++) {
    const token = args[index]!;
    if (sawDoubleDash) {
      // After `--` every token is an operand, even `-input.txt`.
      operands++;
      continue;
    }
    if (token === '--') {
      sawDoubleDash = true;
      continue;
    }
    // A standalone `-` is the stdin input operand, not a flag.
    if (token === '-') {
      operands++;
      continue;
    }
    if (token.startsWith('-')) {
      if (UNIQ_VALUE_FLAGS.has(token)) {
        index++; // skip the option's separate value
      }
      continue;
    }
    operands++;
  }
  // `uniq IN OUT` writes the second operand.
  return operands <= 1;
}

function evaluateRgCommand(args: string[]): boolean {
  for (const token of args) {
    if (
      BLOCKED_RG_FLAGS.includes(token) ||
      BLOCKED_RG_FLAGS.some((flag) => token.startsWith(`${flag}=`))
    ) {
      return false;
    }
  }
  return true;
}

function evaluateGitCommand(tokens: string[]): boolean {
  let index = 1;
  while (index < tokens.length && tokens[index]!.startsWith('-')) {
    const flag = tokens[index]!.toLowerCase();
    if (flag === '--version' || flag === '--help') {
      return true;
    }
    index++;
  }

  if (index >= tokens.length) {
    return true;
  }

  const subcommand = tokens[index]!.toLowerCase();
  if (!READ_ONLY_GIT_SUBCOMMANDS.has(subcommand)) {
    return false;
  }

  const args = tokens.slice(index + 1);

  if (hasGitOutputFlag(args)) {
    return false;
  }

  if (subcommand === 'remote') {
    return evaluateGitRemoteArgs(args);
  }

  if (subcommand === 'branch') {
    return evaluateGitBranchArgs(args);
  }

  return true;
}

function evaluateShellSegment(segment: string): boolean {
  if (!segment.trim()) {
    return true;
  }

  const stripped = stripShellWrapper(segment);
  if (!stripped) {
    return true;
  }

  if (detectCommandSubstitution(stripped)) {
    return false;
  }

  if (containsWriteRedirection(stripped)) {
    return false;
  }

  const tokens = normalizeTokens(stripped);
  if (tokens.length === 0) {
    return true;
  }

  const { root, args } = skipEnvironmentAssignments(tokens);
  if (!root) {
    return true;
  }

  const normalizedRoot = root.toLowerCase();
  if (!READ_ONLY_ROOT_COMMANDS.has(normalizedRoot)) {
    return false;
  }

  if (normalizedRoot === 'find') {
    return evaluateFindCommand([normalizedRoot, ...args]);
  }

  if (normalizedRoot === 'env') {
    return evaluateEnvCommand(args);
  }

  if (normalizedRoot === 'sort') {
    return evaluateSortCommand(args);
  }

  if (normalizedRoot === 'uniq') {
    return evaluateUniqCommand(args);
  }

  if (normalizedRoot === 'rg' || normalizedRoot === 'ripgrep') {
    return evaluateRgCommand(args);
  }

  if (normalizedRoot === 'sed') {
    return evaluateSedCommand([normalizedRoot, ...args]);
  }

  if (normalizedRoot === 'awk') {
    return evaluateAwkCommand([normalizedRoot, ...args]);
  }

  if (normalizedRoot === 'git') {
    return evaluateGitCommand([normalizedRoot, ...args]);
  }

  return true;
}

export function isShellCommandReadOnly(command: string): boolean {
  if (typeof command !== 'string' || !command.trim()) {
    return false;
  }

  const segments = splitCommands(command);

  for (const segment of segments) {
    if (!evaluateShellSegment(segment)) {
      return false;
    }
  }

  return segments.length > 0;
}
