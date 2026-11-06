import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { FirestoreRepository } from '../cloud/repository.mjs';
import { MemoryFirestore } from './firebase-fixture.mjs';
const module = await import('../scripts/firebase-media.mjs').catch(() => ({}));

test('deployment copies only eligible files and finalization cannot resurrect an excluded photo', async t => {
  assert.equal(typeof module.prepareDeployment, 'function');
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-deploy-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const repository = new FirestoreRepository(new MemoryFirestore(), { deferPublication: true });
  const bytes = Buffer.from([255,216,255,0,1]);
  const hash = createHash('sha256').update(bytes).digest('hex');
  const path = `photos/fb_123/${hash}-image.jpg`;
  await mkdir(join(root, 'photos/fb_123'), { recursive: true });
  await writeFile(join(root, path), bytes);
  await repository.registerPhoto({ key: 'fb_123', postId: 'post', assetId: 'asset', selection: 'candidate', members: ['post:asset'] });
  const reservation = await repository.beginPublication('fb_123', hash, path);
  const payload = { objectPath: path, imageHash: hash, size: bytes.length, contentType: 'image/jpeg' };
  await repository.finishPublication('fb_123', reservation, payload);
  await writeFile(join(root, 'manifest.json'), JSON.stringify({ version: 1, photos: [{ key: 'fb_123', reservation, ...payload }] }));
  const snapshot = await module.prepareDeployment({ repository, mediaRoot: root, dist: join(root, 'dist') });
  assert.deepEqual(await readFile(join(root, 'dist/media', path)), bytes);
  assert.equal((await repository.getPhoto('fb_123')).status, 'staged');
  await repository.setExclusion('fb_123', true);
  await module.finalizeDeployment(repository, snapshot);
  assert.equal((await repository.getPhoto('fb_123')).status, 'excluded');
  await module.prepareDeployment({ repository, mediaRoot: root, dist: join(root, 'dist') });
  await assert.rejects(readFile(join(root, 'dist/media', path)));
});

test('media paths reject traversal and credentials rather than copying them into Hosting', async () => {
  assert.equal(typeof module.validMediaPath, 'function');
  for (const path of ['photos/fb_1/../../.env', '.env', 'reference-child/face.jpg', 'photos/fb_1/secret.json', 'photos//x.jpg']) assert.equal(module.validMediaPath(path), false);
  assert.equal(module.validMediaPath('photos/fb_1/abc.jpg'), true);
});

test('a stale media publisher cannot prune a replacement worker file', async t => {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-media-fence-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, 'photos/fb_1'), { recursive: true });
  await writeFile(join(root, 'photos/fb_1/replacement.jpg'), 'replacement');
  const bucket = new module.GitMediaBucket(root);
  let owned = true;
  const repository = { assertOwnership: async () => { if (!owned) throw new Error('lease lost'); }, listPhotos: async () => { owned = false; return []; } };
  await assert.rejects(bucket.commit(repository));
  assert.equal(await readFile(join(root, 'photos/fb_1/replacement.jpg'), 'utf8'), 'replacement');
});

test('the media checkout lock serializes workers and can be released', async t => {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-media-lock-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, '.git'));
  const bucket = new module.GitMediaBucket(root, { git: async () => ({ stdout: '.git/sunnyegg-worker.lock' }) });
  assert.equal(typeof bucket.acquireLock, 'function');
  const release = await bucket.acquireLock();
  await assert.rejects(bucket.acquireLock());
  await release();
  const releaseAgain = await bucket.acquireLock();
  await releaseAgain();
});

test('media initialization clones into an existing empty destination', async t => {
  assert.equal(typeof module.ensureMediaCheckout, 'function');
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-empty-checkout-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  let cloned = false;
  await module.ensureMediaCheckout(root, async () => { cloned = true; await mkdir(join(root, '.git')); });
  assert.equal(cloned, true);
});

test('media initialization preserves a nonempty destination that is not a Git checkout', async t => {
  assert.equal(typeof module.ensureMediaCheckout, 'function');
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-existing-checkout-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await writeFile(join(root, 'personal-file.txt'), 'keep');
  let cloned = false;
  await assert.rejects(module.ensureMediaCheckout(root, async () => { cloned = true; }));
  assert.equal(cloned, false);
  assert.equal(await readFile(join(root, 'personal-file.txt'), 'utf8'), 'keep');
});
