/**
 * @license
 * Copyright 2025 Qwen
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Efficient JSONL (JSON Lines) file utilities.
 *
 * Reading operations:
 * - readLines() - Reads the first N lines efficiently using buffered I/O
 * - read() - Reads entire file into memory as array
 *
 * Writing operations:
 * - writeLine() - Async append with mutex-based concurrency control
 * - writeLineSync() - Sync append (use in non-async contexts)
 * - write() - Overwrites entire file with array of objects
 *
 * Utility operations:
 * - countLines() - Counts non-empty lines
 * - exists() - Checks if file exists and is non-empty
 */

import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { Mutex } from 'async-mutex';

/**
 * A map of file paths to mutexes for preventing concurrent writes.
 */
const fileLocks = new Map<string, Mutex>();

/**
 * Gets or creates a mutex for a specific file path.
 */
function getFileLock(filePath: string): Mutex {
  if (!fileLocks.has(filePath)) {
    fileLocks.set(filePath, new Mutex());
  }
  return fileLocks.get(filePath)!;
}

/**
 * Upper bound on invalid (unparsable, non-empty) lines skipped per read call.
 * Torn trailing records are normal after a crash, but a file made of garbage
 * must not turn the bounded "first N records" read into an unbounded scan.
 */
const MAX_SKIPPED_LINES = 64;

/**
 * Emits one bounded warning per read call; never includes record content.
 */
function warnAboutSkippedLines(
  filePath: string,
  invalidCount: number,
  firstInvalidLine: number | null,
): void {
  if (invalidCount === 0) return;
  const at =
    firstInvalidLine === null ? '' : ` (first at line ${firstInvalidLine})`;
  console.warn(
    `[jsonl] skipped ${invalidCount} invalid line(s) in ${filePath}${at}`,
  );
}

/**
 * True when `filePath` exists, is non-empty, and its last byte is not a
 * line terminator. A torn trailing record (crash mid-append) leaves such a
 * fragment; appending directly to it would glue the new record onto the
 * fragment and corrupt both.
 */
function endsWithoutNewline(filePath: string): boolean {
  let fd: number | undefined;
  try {
    const stats = fs.statSync(filePath);
    if (stats.size === 0) return false;
    fd = fs.openSync(filePath, 'r');
    const buf = Buffer.alloc(1);
    fs.readSync(fd, buf, 0, 1, stats.size - 1);
    const last = buf[0];
    return last !== 0x0a && last !== 0x0d; // '\n' or '\r'
  } catch {
    // Missing or unreadable: nothing to isolate, append as-is.
    return false;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

/**
 * Isolates a torn trailing fragment before an append by terminating the
 * current last line. The fragment becomes its own (skipped) line and the
 * new record starts on a clean line; existing bytes are never rewritten.
 */
function ensureTrailingNewline(filePath: string): void {
  if (endsWithoutNewline(filePath)) {
    fs.appendFileSync(filePath, '\n', 'utf8');
  }
}

/**
 * Reads the first N records from a JSONL file efficiently.
 * Unparsable lines are skipped so a torn trailing record cannot hide the
 * records before it; scanning for further records stops once
 * MAX_SKIPPED_LINES invalid lines have been seen.
 * Returns an array of parsed objects.
 */
export async function readLines<T = unknown>(
  filePath: string,
  count: number,
): Promise<T[]> {
  try {
    const fileStream = fs.createReadStream(filePath);
    const rl = readline.createInterface({
      input: fileStream,
      crlfDelay: Infinity,
    });

    const results: T[] = [];
    let invalidCount = 0;
    let firstInvalidLine: number | null = null;
    let lineNumber = 0;
    for await (const line of rl) {
      lineNumber++;
      if (results.length >= count) break;
      const trimmed = line.trim();
      if (trimmed.length === 0) continue;
      try {
        results.push(JSON.parse(trimmed) as T);
      } catch {
        if (firstInvalidLine === null) firstInvalidLine = lineNumber;
        invalidCount++;
        if (invalidCount >= MAX_SKIPPED_LINES) break;
      }
    }

    warnAboutSkippedLines(filePath, invalidCount, firstInvalidLine);
    return results;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
      console.error(
        `Error reading first ${count} lines from ${filePath}:`,
        error,
      );
    }
    return [];
  }
}

/**
 * Reads all records from a JSONL file.
 * Unparsable lines are skipped; the records that do parse are returned.
 * Returns an array of parsed objects.
 */
export async function read<T = unknown>(filePath: string): Promise<T[]> {
  try {
    const fileStream = fs.createReadStream(filePath);
    const rl = readline.createInterface({
      input: fileStream,
      crlfDelay: Infinity,
    });

    const results: T[] = [];
    let invalidCount = 0;
    let firstInvalidLine: number | null = null;
    let lineNumber = 0;
    for await (const line of rl) {
      lineNumber++;
      const trimmed = line.trim();
      if (trimmed.length === 0) continue;
      try {
        results.push(JSON.parse(trimmed) as T);
      } catch {
        if (firstInvalidLine === null) firstInvalidLine = lineNumber;
        invalidCount++;
      }
    }

    warnAboutSkippedLines(filePath, invalidCount, firstInvalidLine);
    return results;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
      console.error(`Error reading ${filePath}:`, error);
    }
    return [];
  }
}

/**
 * Appends a line to a JSONL file with concurrency control.
 * This method uses a mutex to ensure only one write happens at a time per file.
 */
export async function writeLine(
  filePath: string,
  data: unknown,
): Promise<void> {
  const lock = getFileLock(filePath);
  await lock.runExclusive(() => {
    const line = `${JSON.stringify(data)}\n`;
    // Ensure directory exists before writing
    const dir = path.dirname(filePath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
    ensureTrailingNewline(filePath);
    fs.appendFileSync(filePath, line, 'utf8');
  });
}

/**
 * Synchronous version of writeLine for use in non-async contexts.
 * Uses a simple flag-based locking mechanism (less robust than async version).
 */
export function writeLineSync(filePath: string, data: unknown): void {
  const line = `${JSON.stringify(data)}\n`;
  // Ensure directory exists before writing
  const dir = path.dirname(filePath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  ensureTrailingNewline(filePath);
  fs.appendFileSync(filePath, line, 'utf8');
}

/**
 * Overwrites a JSONL file with an array of objects.
 * Each object will be written as a separate line.
 */
export function write(filePath: string, data: unknown[]): void {
  const lines = data.map((item) => JSON.stringify(item)).join('\n');
  // Ensure directory exists before writing
  const dir = path.dirname(filePath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  fs.writeFileSync(filePath, `${lines}\n`, 'utf8');
}

/**
 * Counts the number of non-empty lines in a JSONL file.
 */
export async function countLines(filePath: string): Promise<number> {
  try {
    const fileStream = fs.createReadStream(filePath);
    const rl = readline.createInterface({
      input: fileStream,
      crlfDelay: Infinity,
    });

    let count = 0;
    for await (const line of rl) {
      if (line.trim().length > 0) {
        count++;
      }
    }
    return count;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
      console.error(`Error counting lines in ${filePath}:`, error);
    }
    return 0;
  }
}

/**
 * Checks if a JSONL file exists and is not empty.
 */
export function exists(filePath: string): boolean {
  try {
    const stats = fs.statSync(filePath);
    return stats.isFile() && stats.size > 0;
  } catch {
    return false;
  }
}
