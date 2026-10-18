/**
 * @license
 * Copyright 2025 Qwen
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * End-to-end recovery tests over the REAL jsonl-utils and filesystem:
 * a session whose file has a torn trailing record or a corrupted middle
 * record must stay loadable, keep recording, and keep previously valid
 * history after reload. The unit-level suites mock jsonl-utils, so they
 * cannot catch the append-path and parent-chain regressions covered here.
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

import { SessionService } from './sessionService.js';
import {
  ChatRecordingService,
  type ChatRecord,
} from './chatRecordingService.js';
import { Storage } from '../config/storage.js';
import type { Config } from '../config/config.js';

vi.mock('../utils/gitUtils.js', () => ({
  getGitBranch: () => undefined,
}));

const SESSION_ID = '550e8400-e29b-41d4-a716-446655440000';

function makeRecord(
  sessionId: string,
  cwd: string,
  uuid: string,
  parentUuid: string | null,
  type: ChatRecord['type'],
  role: 'user' | 'model',
  text: string,
  timestamp: string,
): ChatRecord {
  return {
    uuid,
    parentUuid,
    sessionId,
    timestamp,
    type,
    message: { role, parts: [{ text }] },
    cwd,
    version: 'test',
  };
}

describe('sessionService torn-file recovery (real fs)', () => {
  let fakeHome: string;
  let projectRoot: string;
  let chatsDir: string;
  let sessionFile: string;
  let homedirSpy: MockInstance;

  beforeEach(() => {
    fakeHome = fs.mkdtempSync(path.join(os.tmpdir(), 'cosh-torn-home-'));
    projectRoot = path.join(fakeHome, 'project');
    homedirSpy = vi.spyOn(os, 'homedir').mockReturnValue(fakeHome);
    chatsDir = path.join(new Storage(projectRoot).getProjectDir(), 'chats');
    fs.mkdirSync(chatsDir, { recursive: true });
    sessionFile = path.join(chatsDir, `${SESSION_ID}.jsonl`);
  });

  afterEach(() => {
    homedirSpy.mockRestore();
    fs.rmSync(fakeHome, { recursive: true, force: true });
  });

  function recordingConfig(resumed: unknown): Config {
    return {
      storage: new Storage(projectRoot),
      getSessionId: () => SESSION_ID,
      getCurrentRunId: () => 'run-1',
      getProjectRoot: () => projectRoot,
      getCliVersion: () => 'test',
      getResumedSessionData: () => resumed,
    } as unknown as Config;
  }

  it('keeps old and new messages when recording after a torn tail', async () => {
    const a = makeRecord(
      SESSION_ID,
      projectRoot,
      'a1',
      null,
      'user',
      'user',
      'first',
      '2024-01-01T00:00:00.000Z',
    );
    const b = makeRecord(
      SESSION_ID,
      projectRoot,
      'b1',
      'a1',
      'assistant',
      'model',
      'reply',
      '2024-01-01T00:00:01.000Z',
    );
    // Crash mid-append: the trailing record is a fragment without a newline.
    fs.writeFileSync(
      sessionFile,
      `${JSON.stringify(a)}\n${JSON.stringify(b)}\n{"uuid":`,
      'utf8',
    );

    const sessionService = new SessionService(projectRoot);
    const resumed = await sessionService.loadSession(SESSION_ID);
    expect(resumed).toBeDefined();
    expect(resumed!.conversation.messages.map((m) => m.uuid)).toEqual([
      'a1',
      'b1',
    ]);
    expect(resumed!.lastCompletedUuid).toBe('b1');

    const recorder = new ChatRecordingService(recordingConfig(resumed));
    recorder.recordUserMessage('follow-up');

    const raw = fs.readFileSync(sessionFile, 'utf8');
    const lines = raw.split('\n').filter((l) => l.length > 0);
    // fragment isolated as its own line, new record clean on the next one
    expect(lines).toHaveLength(4);
    const appended = JSON.parse(lines[3]) as ChatRecord;
    expect(appended.parentUuid).toBe('b1');

    const reloaded = await sessionService.loadSession(SESSION_ID);
    expect(reloaded).toBeDefined();
    expect(reloaded!.conversation.messages.map((m) => m.uuid)).toEqual([
      'a1',
      'b1',
      appended.uuid,
    ]);
  });

  it('keeps history before a corrupted middle record', async () => {
    const a = makeRecord(
      SESSION_ID,
      projectRoot,
      'a1',
      null,
      'user',
      'user',
      'first',
      '2024-01-01T00:00:00.000Z',
    );
    const c = makeRecord(
      SESSION_ID,
      projectRoot,
      'c1',
      'b1',
      'assistant',
      'model',
      'after gap',
      '2024-01-01T00:00:02.000Z',
    );
    fs.writeFileSync(
      sessionFile,
      `${JSON.stringify(a)}\n{"uuid":"b1","parentUuid":"a1","message":{ broken\n${JSON.stringify(c)}\n`,
      'utf8',
    );

    const sessionService = new SessionService(projectRoot);
    const loaded = await sessionService.loadSession(SESSION_ID);
    expect(loaded).toBeDefined();
    // The chain bridges the missing 'b1' instead of truncating at it.
    expect(loaded!.conversation.messages.map((m) => m.uuid)).toEqual([
      'a1',
      'c1',
    ]);
  });

  it('still follows parentUuid branches when records are intact', async () => {
    const a = makeRecord(
      SESSION_ID,
      projectRoot,
      'a1',
      null,
      'user',
      'user',
      'first',
      '2024-01-01T00:00:00.000Z',
    );
    const b1 = makeRecord(
      SESSION_ID,
      projectRoot,
      'b1',
      'a1',
      'assistant',
      'model',
      'abandoned branch',
      '2024-01-01T00:00:01.000Z',
    );
    const c1 = makeRecord(
      SESSION_ID,
      projectRoot,
      'c1',
      'b1',
      'user',
      'user',
      'abandoned',
      '2024-01-01T00:00:02.000Z',
    );
    const b2 = makeRecord(
      SESSION_ID,
      projectRoot,
      'b2',
      'a1',
      'assistant',
      'model',
      'active branch',
      '2024-01-01T00:00:03.000Z',
    );
    fs.writeFileSync(
      sessionFile,
      [a, b1, c1, b2].map((r) => JSON.stringify(r)).join('\n') + '\n',
      'utf8',
    );

    const sessionService = new SessionService(projectRoot);
    const loaded = await sessionService.loadSession(SESSION_ID);
    expect(loaded).toBeDefined();
    // No gap: the abandoned b1/c1 branch must NOT be resurrected by
    // file-order bridging; only the active chain is returned.
    expect(loaded!.conversation.messages.map((m) => m.uuid)).toEqual([
      'a1',
      'b2',
    ]);
  });
});
