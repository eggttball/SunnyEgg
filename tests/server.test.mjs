import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Writable } from 'node:stream';
import { once } from 'node:events';
import { createGalleryHandler, parseRange } from '../server.mjs';

class Response extends Writable {
  constructor() {
    super(); this.statusCode = 200; this.headers = new Map(); this.chunks = []; this.headersSent = false;
  }
  setHeader(name, value) { this.headers.set(name.toLowerCase(), String(value)); }
  writeHead(code, headers = {}) {
    this.statusCode = code; this.headersSent = true;
    for (const [name, value] of Object.entries(headers)) this.setHeader(name, value);
  }
  _write(chunk, encoding, done) { this.chunks.push(Buffer.from(chunk)); done(); }
}

async function fixture(t, manifest = { version: 1, posts: [], fetchStatus: { state: 'blocked' } }) {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-server-'));
  for (const dir of ['public', 'data/media/post1', 'data/clips', 'data/reference-child']) {
    await mkdir(join(root, dir), { recursive: true });
  }
  await writeFile(join(root, 'public/index.html'), '<!doctype html><title>SunnyEgg test</title>');
  await writeFile(join(root, 'data/manifest.json'), JSON.stringify(manifest));
  await writeFile(join(root, 'data/media/post1/test.mp4'), Buffer.from('0123456789'));
  await writeFile(join(root, 'data/reference-child/child.jpg'), Buffer.from('private reference'));
  await writeFile(join(root, '.env'), 'FACEBOOK_TOKEN=private-secret');
  await symlink(join(root, 'data/reference-child/child.jpg'), join(root, 'data/media/leak.jpg'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const handler = createGalleryHandler({ root });
  const request = async (path, options = {}) => {
    const response = new Response();
    const done = once(response, 'finish');
    const headers = { host: '127.0.0.1:4173' };
    for (const [name, value] of Object.entries(options.headers || {})) headers[name.toLowerCase()] = value;
    await handler({ url: path, method: options.method || 'GET', headers }, response);
    await done;
    const body = Buffer.concat(response.chunks);
    return { status: response.statusCode, headers: { get: name => response.headers.get(name.toLowerCase()) }, text: async () => body.toString(), json: async () => JSON.parse(body.toString()) };
  };
  request.readManifest = async () => JSON.parse(await readFile(join(root, 'data/manifest.json'), 'utf8'));
  return request;
}

test('gallery API counts references without exposing their names or bytes', async t => {
  const request = await fixture(t);
  const response = await request('/api/gallery');
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.reference.count, 1);
  assert.equal(data.reference.directory, 'data/reference-child');
  assert.deepEqual(data.posts, []);
  assert.equal(JSON.stringify(data).includes('child.jpg'), false);
});

test('page responses block all frames and media players while keeping scripts and requests local', async t => {
  const request = await fixture(t);
  const response = await request('/');
  const directives = new Map(response.headers.get('content-security-policy').split(';').map(part => {
    const [name, ...sources] = part.trim().split(/\s+/);
    return [name, sources];
  }));
  assert.deepEqual(directives.get('frame-src'), ["'none'"]);
  assert.deepEqual(directives.get('media-src'), ["'none'"]);
  for (const type of ['script-src', 'connect-src']) {
    assert.deepEqual(directives.get(type), ["'self'"]);
  }
  assert.deepEqual(directives.get('frame-ancestors'), ["'none'"]);
});

test('video range requests return exact bytes and content range', async t => {
  const request = await fixture(t);
  const response = await request('/media/post1/test.mp4', { headers: { Range: 'bytes=2-5' } });
  assert.equal(response.status, 206);
  assert.equal(response.headers.get('content-range'), 'bytes 2-5/10');
  assert.equal(response.headers.get('content-type'), 'video/mp4');
  assert.equal(await response.text(), '2345');
  const suffix = await request('/media/post1/test.mp4', { headers: { Range: 'bytes=-3' } });
  assert.equal(await suffix.text(), '789');
  const invalid = await request('/media/post1/test.mp4', { headers: { Range: 'bytes=99-' } });
  assert.equal(invalid.status, 416);
  assert.equal(invalid.headers.get('content-range'), 'bytes */10');
});

test('private references, arbitrary project files and symlink escapes are denied', async t => {
  const request = await fixture(t);
  for (const path of ['/data/reference-child/child.jpg', '/reference-child/child.jpg', '/data/manifest.json', '/server.mjs', '/.env', '/media/leak.jpg', '/media/%2e%2e%2freference-child%2fchild.jpg']) {
    const response = await request(path);
    assert.ok([403, 404].includes(response.status), `${path}: ${response.status}`);
    assert.equal((await response.text()).includes('private reference'), false);
  }
});

test('gallery API excludes legacy videos and video data within photo posts without changing the manifest', async t => {
  const manifest = { version: 1, posts: [
    { id: 'photos', kind: 'photos', assets: [
      { id: 'image', type: 'image', path: 'media/image.jpg' },
      { id: 'stale-video', type: 'video', path: 'media/stale.mp4' },
    ], recognition: { state: 'complete', results: [
      { assetId: 'image', state: 'candidate' }, { assetId: 'stale-video', state: 'candidate' },
    ] }, clips: [{ type: 'video', path: 'clips/stale.mp4' }] },
    { id: 'legacy-reel', kind: 'video', playback: 'facebook_embed', sourceUrl: 'https://www.facebook.com/reel/123/' },
  ], jobStatus: { state: 'complete', simulated: true, counts: { inspectedPhotos: 12, noFacePhotos: 12 }, message: '已完成模擬' } };
  const request = await fixture(t, manifest);
  const response = await request('/api/gallery');
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.deepEqual(data.posts.map(post => post.id), ['photos']);
  assert.deepEqual(data.posts[0].assets, [{ id: 'image', type: 'image', path: 'media/image.jpg' }]);
  assert.deepEqual(data.posts[0].recognition.results, [{ assetId: 'image', state: 'candidate' }]);
  assert.equal(Object.hasOwn(data.posts[0], 'clips'), false);
  assert.doesNotMatch(JSON.stringify(data.posts), /stale.mp4|legacy-reel|facebook_embed/);
  assert.deepEqual(data.jobStatus, manifest.jobStatus);
  assert.deepEqual(await request.readManifest(), manifest);
});

test('missing and malformed manifests produce clear failures without exposing private data', async t => {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-manifest-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const handler = createGalleryHandler({ root });
  async function request() {
    const response = new Response();
    const done = once(response, 'finish');
    await handler({ url: '/api/gallery', method: 'GET', headers: { host: 'localhost:4173' } }, response);
    await done;
    return { status: response.statusCode, body: Buffer.concat(response.chunks).toString() };
  }
  const missing = await request();
  assert.equal(missing.status, 404);
  await mkdir(join(root, 'data'));
  await writeFile(join(root, 'data/manifest.json'), '{ private-secret');
  const malformed = await request();
  assert.equal(malformed.status, 500);
  assert.match(malformed.body, /相簿資料讀取失敗/);
  assert.doesNotMatch(malformed.body, /private-secret/);
});

test('rejects Host values from unrelated origins', async t => {
  const request = await fixture(t);
  const response = await request('/api/gallery', { headers: { Host: 'attacker.example' } });
  assert.equal(response.status, 403);
});

test('parses bounded, open and suffix ranges and rejects malformed ranges', () => {
  assert.deepEqual(parseRange('bytes=2-20', 10), { start: 2, end: 9 });
  assert.deepEqual(parseRange('bytes=3-', 10), { start: 3, end: 9 });
  assert.deepEqual(parseRange('bytes=-20', 10), { start: 0, end: 9 });
  for (const value of ['bytes=5-2', 'bytes=-0', 'bytes=1-2,4-5', 'bytes=abc', 'bytes=20-', 'bytes=999999999999999999999-']) {
    assert.equal(parseRange(value, 10), null, value);
  }
  assert.equal(parseRange('bytes=0-', 0), null);
});
