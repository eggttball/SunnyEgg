import assert from 'node:assert/strict';
import test from 'node:test';
import { chmod, mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createManualPhotoController, createManualPhotoRunner } from '../scripts/manual-photo.mjs';

async function terminal(controller) {
  for (let attempt = 0; attempt < 20; attempt++) {
    await new Promise(resolve => setImmediate(resolve));
    if (controller.getStatus().state !== 'running') return controller.getStatus();
  }
  assert.fail('Manual photo import did not finish');
}

test('manual photo status reports the verified like separately from saving', async () => {
  const controller = createManualPhotoController({ root: '/unused', runner: async () => ({
    state: 'complete', action: 'added', reaction: 'liked', token: 'private-secret',
  }) });
  controller.start({ url: 'https://www.facebook.com/photo/?fbid=100' });
  const status = await terminal(controller);
  assert.equal(status.reaction, 'liked');
  assert.match(status.message, /已.*按讚/);
  assert.doesNotMatch(JSON.stringify(status), /private-secret|未執行.*按讚/);
});

test('a saved photo with a failed like has a retryable partial status', async () => {
  const controller = createManualPhotoController({ root: '/unused', runner: async () => ({
    state: 'partial', action: 'added', reaction: 'failed', reason: 'reaction_failure',
  }) });
  controller.start({ url: 'https://www.facebook.com/photo/?fbid=100' });
  const status = await terminal(controller);
  assert.equal(status.state, 'partial');
  assert.equal(status.action, 'added');
  assert.match(status.message, /按讚.*失敗|按讚.*未成功/);
  assert.match(status.message, /重試/);
});

test('disabled auto like is reported without claiming a like succeeded', async () => {
  const controller = createManualPhotoController({ root: '/unused', runner: async () => ({
    state: 'complete', action: 'already_selected', reaction: 'not_requested',
  }) });
  controller.start({ url: 'https://www.facebook.com/photo/?fbid=100' });
  const status = await terminal(controller);
  assert.equal(status.reaction, 'not_requested');
  assert.match(status.message, /自動按讚.*關閉/);
});

test('a saved photo with an unreadable like record gives storage guidance', async () => {
  const controller = createManualPhotoController({ root: '/unused', runner: async () => ({
    state: 'partial', action: 'added', reaction: 'failed', reason: 'local_storage',
  }) });
  controller.start({ url: 'https://www.facebook.com/photo/?fbid=100' });
  const status = await terminal(controller);
  assert.equal(status.state, 'partial');
  assert.match(status.message, /儲存|權限/);
  assert.doesNotMatch(status.message, /登入/);
});

test('runner preserves a partial save result from a nonzero Python exit', async t => {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg manual photo '));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, '.venv/bin'), { recursive: true });
  await mkdir(join(root, 'scripts'));
  const python = join(root, '.venv/bin/python');
  await writeFile(python, '#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps({"state":"partial","action":"added","reaction":"failed"}))\nsys.exit(2)\n');
  await chmod(python, 0o700);
  const result = await createManualPhotoRunner({ root })({ photoId: '100' });
  assert.deepEqual(result, { state: 'partial', action: 'added', reaction: 'failed' });
});
