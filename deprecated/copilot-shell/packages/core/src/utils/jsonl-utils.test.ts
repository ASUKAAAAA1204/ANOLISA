/**
 * @license
 * Copyright 2025 Qwen
 * SPDX-License-Identifier: Apache-2.0
 */

import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
  type MockInstance,
} from 'vitest';

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { read, readLines, writeLine, writeLineSync } from './jsonl-utils.js';

describe('jsonl-utils torn-line tolerance', () => {
  let tempDir: string;
  let warnSpy: MockInstance;

  beforeEach(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jsonl-utils-test-'));
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    warnSpy.mockRestore();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  const writeFixture = (name: string, content: string): string => {
    const filePath = path.join(tempDir, name);
    fs.writeFileSync(filePath, content, 'utf8');
    return filePath;
  };

  it('read returns records before a torn trailing line', async () => {
    const filePath = writeFixture('torn-tail.jsonl', '{"a":1}\n{"b":2}\n{"c":');
    await expect(read(filePath)).resolves.toEqual([{ a: 1 }, { b: 2 }]);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0][0]).toContain(filePath);
    expect(warnSpy.mock.calls[0][0]).toContain('(first at line 3)');
  });

  it('read returns records around mid-file corruption', async () => {
    const filePath = writeFixture(
      'mid-corrupt.jsonl',
      '{"a":1}\nnot json at all\n\n{"b":2}\n',
    );
    await expect(read(filePath)).resolves.toEqual([{ a: 1 }, { b: 2 }]);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0][0]).toContain('(first at line 2)');
  });

  it('read returns an empty array only when nothing is readable', async () => {
    const filePath = writeFixture('all-bad.jsonl', 'garbage\nmore garbage\n');
    await expect(read(filePath)).resolves.toEqual([]);
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it('read stays silent when every line parses', async () => {
    const filePath = writeFixture('clean.jsonl', '{"a":1}\n{"b":2}\n');
    await expect(read(filePath)).resolves.toEqual([{ a: 1 }, { b: 2 }]);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('readLines skips invalid lines while collecting valid records', async () => {
    const filePath = writeFixture(
      'bad-then-good.jsonl',
      '{"torn":\n{"a":1}\n{"b":2}\n',
    );
    await expect(readLines(filePath, 2)).resolves.toEqual([{ a: 1 }, { b: 2 }]);
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it('readLines keeps blank lines out of the invalid budget', async () => {
    const blank = Array.from({ length: 120 }, () => '').join('\n');
    const filePath = writeFixture('blanks.jsonl', `${blank}\n{"a":1}\n`);
    await expect(readLines(filePath, 1)).resolves.toEqual([{ a: 1 }]);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('readLines stops scanning after unbounded invalid-line storms', async () => {
    const storm = Array.from({ length: 200 }, (_, i) => `bad line ${i}`).join(
      '\n',
    );
    const filePath = writeFixture('storm.jsonl', `${storm}\n{"a":1}\n`);
    // The valid record sits far beyond the skip budget, so the bounded read
    // must stop early instead of scanning to EOF.
    await expect(readLines(filePath, 1)).resolves.toEqual([]);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0][0]).toMatch(/skipped \d+ invalid line\(s\)/);
  });
});

describe('jsonl-utils append after torn tail', () => {
  let tempDir: string;
  let warnSpy: MockInstance;

  beforeEach(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jsonl-append-test-'));
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    warnSpy.mockRestore();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  const filePathFor = (name: string): string => path.join(tempDir, name);

  it('writeLineSync isolates a torn trailing fragment', async () => {
    const filePath = filePathFor('torn.jsonl');
    fs.writeFileSync(filePath, '{"a":1}\n{"uuid":', 'utf8');
    writeLineSync(filePath, { b: 2 });
    // The fragment gets its own terminated line; the new record is clean.
    expect(fs.readFileSync(filePath, 'utf8')).toBe(
      '{"a":1}\n{"uuid":\n{"b":2}\n',
    );
    await expect(read(filePath)).resolves.toEqual([{ a: 1 }, { b: 2 }]);
  });

  it('writeLine isolates a torn trailing fragment', async () => {
    const filePath = filePathFor('torn-async.jsonl');
    fs.writeFileSync(filePath, '{"a":1}\n{"uuid":', 'utf8');
    await writeLine(filePath, { b: 2 });
    expect(fs.readFileSync(filePath, 'utf8')).toBe(
      '{"a":1}\n{"uuid":\n{"b":2}\n',
    );
    await expect(read(filePath)).resolves.toEqual([{ a: 1 }, { b: 2 }]);
  });

  it('writeLineSync does not add a newline to a well-terminated file', () => {
    const filePath = filePathFor('clean.jsonl');
    fs.writeFileSync(filePath, '{"a":1}\n', 'utf8');
    writeLineSync(filePath, { b: 2 });
    expect(fs.readFileSync(filePath, 'utf8')).toBe('{"a":1}\n{"b":2}\n');
  });

  it('writeLineSync does not touch a CRLF-terminated tail', () => {
    const filePath = filePathFor('crlf.jsonl');
    fs.writeFileSync(filePath, '{"a":1}\r\n', 'utf8');
    writeLineSync(filePath, { b: 2 });
    expect(fs.readFileSync(filePath, 'utf8')).toBe('{"a":1}\r\n{"b":2}\n');
  });

  it('writeLineSync creates a fresh file without a leading newline', () => {
    const filePath = filePathFor('new.jsonl');
    writeLineSync(filePath, { a: 1 });
    expect(fs.readFileSync(filePath, 'utf8')).toBe('{"a":1}\n');
  });
});
