import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import http from 'node:http';
import { createGalleryServer } from '../server.mjs';

async function fixture(t, { runner = async () => ({ state: 'complete' }), now = '2026-10-31T16:00:00.000Z' } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-manual-http-'));
  await mkdir(join(root, 'public'));
  await writeFile(join(root, 'public/index.html'), '<title>fixture</title>');
  const server = createGalleryServer({ root, manualFetchRunner: runner, clock: () => new Date(now) });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const request = (path, options = {}) => new Promise((resolve, reject) => {
    const request = http.request(base + path, { method: options.method || 'GET', headers: options.headers }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => {
        const body = Buffer.concat(chunks).toString();
        resolve({ status: response.statusCode, json: async () => JSON.parse(body), text: async () => body });
      });
    });
    request.on('error', reject);
    request.end(options.body);
  });
  const post = (body, headers = {}) => request('/api/manual-fetch', {
    method: 'POST', headers: { Origin: base, 'Content-Type': 'application/json', ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  return { request, post, base };
}

async function terminal(request) {
  for (let attempt = 0; attempt < 20; attempt++) {
    const status = await (await request('/api/manual-fetch/status')).json();
    if (status.state !== 'running') return status;
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.fail('manual-fetch did not finish');
}

test('manual options use Taipei today and include today in day presets', async t => {
  const { request } = await fixture(t);
  const response = await request('/api/manual-fetch/options');
  assert.equal(response.status, 200);
  const options = await response.json();
  assert.equal(options.timeZone, 'Asia/Taipei');
  assert.equal(options.today, '2026-11-01');
  assert.equal(options.minDate, '2026-08-01');
  assert.equal(options.maxDate, '2026-11-01');
  assert.deepEqual(options.defaultRange, { startDate: '2026-10-30', endDate: '2026-11-01' });
  assert.deepEqual(options.presets.map(({ value, startDate, endDate }) => ({ value, startDate, endDate })), [
    { value: '3d', startDate: '2026-10-30', endDate: '2026-11-01' },
    { value: '7d', startDate: '2026-10-26', endDate: '2026-11-01' },
    { value: '14d', startDate: '2026-10-19', endDate: '2026-11-01' },
    { value: '1m', startDate: '2026-10-01', endDate: '2026-11-01' },
    { value: '3m', startDate: '2026-08-01', endDate: '2026-11-01' },
  ]);
  assert.ok(options.presets.every(item => typeof item.label === 'string' && item.label.length));
});

test('calendar month subtraction clamps to the last day across leap years', async t => {
  const { request } = await fixture(t, { now: '2024-05-30T16:00:00Z' });
  const options = await (await request('/api/manual-fetch/options')).json();
  assert.equal(options.today, '2024-05-31');
  assert.equal(options.minDate, '2024-02-29');
  assert.equal(options.presets.find(item => item.value === '1m').startDate, '2024-04-30');
});

test('idle status does not expose an unrelated previous gallery job', async t => {
  const { request } = await fixture(t);
  const response = await request('/api/manual-fetch/status');
  assert.equal(response.status, 200);
  const status = await response.json();
  assert.equal(status.state, 'idle');
  assert.equal(Object.hasOwn(status, 'counts'), false);
  assert.equal(Object.hasOwn(status, 'runId'), false);
});

test('unexpected manual API errors return a generic message without file paths', async t => {
  const { request } = await fixture(t, { now: 'invalid clock' });
  const response = await request('/api/manual-fetch/options');
  assert.equal(response.status, 500);
  assert.doesNotMatch(await response.text(), /manifest|\.env|\/Users\//);
});

test('valid POST starts asynchronously and concurrent duplicate POSTs cannot start another run', async t => {
  let finish;
  const completed = new Promise(resolve => { finish = resolve; });
  const invocations = [];
  const { request, post } = await fixture(t, { runner: range => { invocations.push(range); return completed; } });
  const range = { startDate: '2026-08-01', endDate: '2026-11-01' };
  const responses = await Promise.all([post(range), post(range)]);
  assert.deepEqual(responses.map(response => response.status).sort(), [202, 409]);
  const started = await responses.find(response => response.status === 202).json();
  const duplicate = await responses.find(response => response.status === 409).json();
  assert.equal(started.state, 'running');
  assert.deepEqual(started.range, { ...range, timeZone: 'Asia/Taipei' });
  assert.equal(started.startedAt, '2026-10-31T16:00:00.000Z');
  assert.equal(typeof started.runId, 'string');
  assert.equal(duplicate.status.state, 'running');
  assert.equal(duplicate.status.runId, started.runId);
  assert.equal(invocations.length, 1);
  assert.deepEqual(invocations[0], { ...range, timeZone: 'Asia/Taipei', runId: started.runId });
  assert.equal((await (await request('/api/manual-fetch/status')).json()).state, 'running');
  finish({ state: 'complete', counts: { discoveredPosts: 2, inspectedPhotos: 4, candidatePhotos: 1 } });
  const finished = await terminal(request);
  assert.equal(finished.state, 'complete');
  assert.equal(finished.runId, started.runId);
  assert.deepEqual(finished.counts, { discoveredPosts: 2, inspectedPhotos: 4, candidatePhotos: 1 });
  assert.equal(finished.finishedAt, '2026-10-31T16:00:00.000Z');
});

test('date validation rejects invalid calendar dates, reversed ranges and dates beyond allowed bounds', async t => {
  const { post } = await fixture(t, { runner: () => { assert.fail('invalid input started a runner'); } });
  for (const body of [
    {}, null, [], 'not json',
    { startDate: '2026-02-30', endDate: '2026-11-01' },
    { startDate: '2026-09-31', endDate: '2026-11-01' },
    { startDate: '2026-8-01', endDate: '2026-11-01' },
    { startDate: '2026-08-01T00:00:00Z', endDate: '2026-11-01' },
    { startDate: '2026-07-31', endDate: '2026-11-01' },
    { startDate: '2026-11-01', endDate: '2026-11-02' },
    { startDate: '2026-10-31', endDate: '2026-10-30' },
    { startDate: '2026-08-01', endDate: 20261101 },
    { startDate: '2026-08-01', endDate: '2026-11-01', executable: 'evil' },
  ]) {
    assert.equal((await post(body)).status, 400, JSON.stringify(body));
  }
});

test('POST requires exact same origin, JSON and safe browser site metadata', async t => {
  const { post, base } = await fixture(t, { runner: () => { assert.fail('rejected request started a runner'); } });
  const body = { startDate: '2026-10-30', endDate: '2026-11-01' };
  for (const [headers, expected] of [
    [{ Origin: '' }, 403], [{ Origin: 'null' }, 403],
    [{ Origin: 'https://attacker.example' }, 403],
    [{ Origin: base.replace('127.0.0.1', 'localhost') }, 403],
    [{ 'Sec-Fetch-Site': 'cross-site' }, 403],
    [{ 'Content-Type': 'text/plain' }, 415],
    [{ Host: 'attacker.example' }, 403],
  ]) assert.equal((await post(body, headers)).status, expected);
});

test('POST rejects oversized bodies and unrelated mutation routes keep their method guard', async t => {
  const { request, post } = await fixture(t, { runner: () => { assert.fail('oversized input started a runner'); } });
  assert.equal((await post(' '.repeat(8193))).status, 413);
  assert.equal((await request('/api/gallery', { method: 'POST', body: '{}' })).status, 405);
  assert.equal((await request('/api/manual-fetch/status', { method: 'POST', body: '{}' })).status, 405);
});

test('streamed POST bodies are bounded even without Content-Length', async t => {
  const { base } = await fixture(t, { runner: () => { assert.fail('oversized streamed body started a runner'); } });
  const response = await new Promise((resolve, reject) => {
    const request = http.request(base + '/api/manual-fetch', {
      method: 'POST', headers: { Origin: base, 'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked' },
    }, response => {
      response.resume();
      response.on('end', () => { request.end(); resolve(response); });
    });
    request.on('error', reject);
    request.write(' '.repeat(4096));
    request.write(' '.repeat(4096));
    request.write(' ');
  });
  assert.equal(response.statusCode, 413);
});

test('terminal statuses whitelist report fields, counts and safe messages', async t => {
  const { request, post } = await fixture(t, { runner: async () => ({
    state: 'partial', reason: 'scan_limit', message: 'token=private-secret /Users/private/.env',
    counts: { inspectedPhotos: 8, candidatePhotos: 2, likedPhotos: 2, likedPosts: 1, outOfRangePosts: 3, missingDatePosts: 1, unprocessedObservations: 4, failedPosts: -1, noFacePhotos: 0.2, tokens: 999 },
    runId: '/Users/private/path', cookies: ['private-secret'], posts: [{ secret: 'private-secret' }],
    startedAt: 'private-secret', stdout: 'private-secret', stderr: 'private-secret',
  }) });
  assert.equal((await post({ startDate: '2026-10-30', endDate: '2026-11-01' })).status, 202);
  const status = await terminal(request);
  assert.equal(status.state, 'partial');
  assert.deepEqual(status.counts, { inspectedPhotos: 8, candidatePhotos: 2, likedPhotos: 2, likedPosts: 1, outOfRangePosts: 3, missingDatePosts: 1, unprocessedObservations: 4 });
  assert.deepEqual(Object.keys(status).sort(), ['counts', 'finishedAt', 'message', 'range', 'runId', 'startedAt', 'state']);
  assert.doesNotMatch(JSON.stringify(status), /private-secret|\/Users\/private|tokens|cookies|stdout|stderr/);
  assert.match(status.message, /掃描上限/);
});

test('incomplete feed and unavailable page results explain their safe cause', async t => {
  for (const { state, reason, message } of [
    { state: 'partial', reason: 'feed_stalled', message: /日期範圍尚未完整/ },
    { state: 'blocked', reason: 'page_unavailable', message: /專頁目前無法讀取/ },
    { state: 'blocked', reason: 'feed_unavailable', message: /貼文列表目前無法讀取/ },
    { state: 'partial', reason: 'rate_limit', message: /Facebook.*API.*使用量.*暫停.*稍後/ },
    { state: 'blocked', reason: 'feedback_unavailable', message: /本機.*排除紀錄.*負例照片.*無法讀取/ },
    { state: 'partial', reason: 'feedback_changed', message: /排除標記已變更.*暫停.*重新抓取/ },
    { state: 'partial', reason: 'reaction_failure', message: /候選照片已保存.*照片按讚.*未確認成功/ },
  ]) {
    const { request, post } = await fixture(t, { runner: async () => ({ state, reason }) });
    await post({ startDate: '2026-10-30', endDate: '2026-11-01' });
    const status = await terminal(request);
    assert.equal(status.state, state);
    assert.match(status.message, message);
  }
});

test('browser setup and launch failures return actionable safe messages through the manual API', async t => {
  for (const { reason, message } of [
    { reason: 'browser_dependency_missing', message: /Playwright.*SunnyEgg.*終端機.*npm install/ },
    { reason: 'browser_missing', message: /Chromium.*SunnyEgg.*終端機.*npx playwright install chromium/ },
    { reason: 'profile_missing', message: /專用.*Facebook.*登入/ },
    { reason: 'profile_unavailable', message: /設定檔.*權限/ },
    { reason: 'profile_in_use', message: /關閉.*整個.*專用瀏覽器/ },
    { reason: 'browser_launch_failed', message: /瀏覽器.*啟動失敗/ },
    { reason: 'facebook_navigation_failed', message: /瀏覽器已開啟.*Facebook.*連線失敗/ },
    { reason: 'login_cancelled', message: /登入.*取消/ },
    { reason: 'invalid_arguments', message: /指令.*參數.*無效/ },
  ]) {
    await t.test(reason, async t => {
      const { request, post } = await fixture(t, { runner: async () => ({
        state: 'blocked', reason, message: 'token=private-secret /Users/private/browser-profile',
        stdout: 'private-secret', stderr: 'private-secret', profile: '/Users/private/browser-profile',
      }) });
      assert.equal((await post({ startDate: '2026-10-30', endDate: '2026-11-01' })).status, 202);
      const status = await terminal(request);
      assert.equal(status.state, 'blocked');
      assert.match(status.message, message);
      assert.deepEqual(Object.keys(status).sort(), ['finishedAt', 'message', 'range', 'runId', 'startedAt', 'state']);
      assert.doesNotMatch(JSON.stringify(status), /private-secret|\/Users\/private|stdout|stderr/);
    });
  }
});

test('batch lock reports blocked and execution errors use a generic failed status', async t => {
  let call = 0;
  const { request, post } = await fixture(t, { runner: async () => {
    if (!call++) return { state: 'blocked', reason: 'batch_locked' };
    throw new Error('private-secret /Users/private/.env');
  } });
  const range = { startDate: '2026-10-30', endDate: '2026-11-01' };
  assert.equal((await post(range)).status, 202);
  const blocked = await terminal(request);
  assert.equal(blocked.state, 'blocked');
  assert.match(blocked.message, /執行中/);
  assert.equal((await post(range)).status, 202);
  const failed = await terminal(request);
  assert.equal(failed.state, 'failed');
  assert.doesNotMatch(JSON.stringify(failed), /private-secret|\/Users\/private/);
});
