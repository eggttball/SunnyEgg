import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createManualFetchRunner } from '../scripts/manual-fetch.mjs';

async function fixture(t, program) {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg runner '));
  await mkdir(join(root, '.venv/bin'), { recursive: true });
  await mkdir(join(root, 'scripts'));
  await writeFile(join(root, 'scripts/daily.py'), 'fixture, never a real batch');
  const python = join(root, '.venv/bin/python');
  await writeFile(python, '#!/usr/bin/env python3\nimport json, sys\n' + program);
  await chmod(python, 0o700);
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

test('runner uses fixed project interpreter, script and environment path with validated dates', async t => {
  const root = await fixture(t, `sys.stderr.write('private-secret')\nprint(json.dumps({'state':'complete', 'args':sys.argv[1:]}))`);
  const runner = createManualFetchRunner({ root });
  const report = await runner({ startDate: '2026-08-01', endDate: '2026-11-01', timeZone: 'Asia/Taipei', runId: 'manual-fixture' });
  assert.equal(report.state, 'complete');
  assert.deepEqual(report.args, [
    join(root, 'scripts/daily.py'), '--root', root, '--env-file', join(root, '.env'),
    '--start-date', '2026-08-01', '--end-date', '2026-11-01', '--max-scrolls', '100',
  ]);
});

test('runner preserves structured blocked results from a nonzero Python exit', async t => {
  const root = await fixture(t, `print(json.dumps({'state':'blocked','reason':'batch_locked'}))\nsys.exit(1)`);
  const report = await createManualFetchRunner({ root })({ startDate: '2026-10-30', endDate: '2026-11-01' });
  assert.deepEqual(report, { state: 'blocked', reason: 'batch_locked' });
});

test('runner never includes raw subprocess output in malformed-output errors', async t => {
  const root = await fixture(t, `sys.stderr.write('private-secret /Users/private')\nprint('private-secret')\nsys.exit(1)`);
  await assert.rejects(createManualFetchRunner({ root })({ startDate: '2026-10-30', endDate: '2026-11-01' }), error => {
    assert.doesNotMatch(error.message, /private-secret|\/Users\/private/);
    return true;
  });
});

test('runner rejects malformed dates before executing the subprocess', async t => {
  const root = await fixture(t, `print(json.dumps({'state':'complete'}))`);
  await assert.rejects(createManualFetchRunner({ root })({ startDate: '2026-11-01; touch evil', endDate: '2026-11-01' }));
});

test('runner falls back to python3 when a project interpreter is absent', async t => {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-python-fallback-'));
  await mkdir(join(root, 'scripts'));
  await writeFile(join(root, 'scripts/daily.py'), "import json\nprint(json.dumps({'state':'complete'}))");
  t.after(() => rm(root, { recursive: true, force: true }));
  const report = await createManualFetchRunner({ root })({ startDate: '2026-10-30', endDate: '2026-11-01' });
  assert.equal(report.state, 'complete');
});

test('runner rejects excessive stdout with a generic error', async t => {
  const root = await fixture(t, "sys.stdout.write('X' * (8 * 1024 * 1024 + 1))");
  await assert.rejects(createManualFetchRunner({ root })({ startDate: '2026-10-30', endDate: '2026-11-01' }), error => {
    assert.equal(error.message.includes('X'), false);
    return true;
  });
});
