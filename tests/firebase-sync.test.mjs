import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, writeFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { FirestoreRepository } from '../cloud/repository.mjs';
import { syncGallery, deleteExcludedObjects } from '../scripts/firebase-sync.mjs';
import { MemoryFirestore, MemoryBucket } from './firebase-fixture.mjs';

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-firebase-sync-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, 'data/media/post'), { recursive: true });
  const assets = ['candidate', 'no_match', 'no_face', 'candidate'].map((state, n) => ({ id: 'photo' + n, type: 'image', path: `media/post/${n}.jpg`, sourcePhotoId: String(100 + n), ...(n === 3 ? { userExcluded: true } : {}) }));
  for (let n = 0; n < assets.length; n++) await writeFile(join(root, 'data', assets[n].path), Buffer.from([255, 216, 255, n, 255, 217]));
  const manifest = { version: 1, posts: [{ id: 'post', kind: 'photos', title: '午餐', publishedAt: '2026-11-05', sourceUrl: 'https://www.facebook.com/1/posts/2', assets, recognition: { results: assets.map((a, n) => ({ assetId: a.id, state: ['candidate', 'no_match', 'no_face', 'candidate'][n] })) } }] };
  await writeFile(join(root, 'data/manifest.json'), JSON.stringify(manifest));
  return { root, manifest, repository: new FirestoreRepository(new MemoryFirestore()), bucket: new MemoryBucket() };
}

test('sync uploads only selected originals and a repeated sync does not create another object', async t => {
  const env = await fixture(t);
  await syncGallery(env);
  assert.equal(env.bucket.objects.size, 1);
  assert.equal((await env.repository.getPhoto('fb_100')).status, 'published');
  assert.equal(await env.repository.getPhoto('fb_101'), null);
  assert.equal((await env.repository.getPhoto('fb_103')).userExcluded, true);
  await syncGallery(env);
  assert.equal(env.bucket.objects.size, 1);
});

test('an administrator exclusion deletes the original and cannot be undone by a stale local manifest', async t => {
  const env = await fixture(t);
  await syncGallery(env);
  await env.repository.setExclusion('fb_100', true);
  await deleteExcludedObjects(env.repository, env.bucket);
  assert.equal(env.bucket.objects.size, 0);
  await syncGallery(env);
  assert.equal(env.bucket.objects.size, 0);
  assert.equal((await env.repository.getPhoto('fb_100')).userExcluded, true);
});

test('an exclusion during upload removes the uploaded bytes instead of publishing stale metadata', async t => {
  const env = await fixture(t);
  env.bucket.onSave = async () => env.repository.setExclusion('fb_100', true);
  await syncGallery(env);
  assert.equal(env.bucket.objects.size, 0);
  assert.equal((await env.repository.getPhoto('fb_100')).status, 'excluded');
});

test('failed Storage deletion stays queued for retry and gallery visibility is already removed', async t => {
  const env = await fixture(t);
  await syncGallery(env);
  await env.repository.setExclusion('fb_100', true);
  env.bucket.failDelete = true;
  await assert.rejects(deleteExcludedObjects(env.repository, env.bucket));
  assert.equal((await env.repository.getPhoto('fb_100')).objectsToDelete.length, 1);
  env.bucket.failDelete = false;
  await deleteExcludedObjects(env.repository, env.bucket);
  assert.equal(env.bucket.objects.size, 0);
  assert.deepEqual((await env.repository.getPhoto('fb_100')).objectsToDelete, []);
});

test('missing manifests and symlinked originals never publish or erase existing cloud photos', async t => {
  const env = await fixture(t);
  await syncGallery(env);
  await rm(join(env.root, 'data/manifest.json'));
  await assert.rejects(syncGallery(env));
  assert.equal(env.bucket.objects.size, 1);
  await writeFile(join(env.root, 'data/manifest.json'), JSON.stringify(env.manifest));
  await rm(join(env.root, 'data/media/post/0.jpg'));
  await symlink(join(env.root, 'data/media/post/1.jpg'), join(env.root, 'data/media/post/0.jpg'));
  await assert.rejects(syncGallery(env));
  assert.equal(env.bucket.objects.size, 1);
});

test('restoring a cloud exclusion makes the local original eligible for upload again', async t => {
  const env = await fixture(t);
  await syncGallery(env);
  await env.repository.setExclusion('fb_100', true);
  await deleteExcludedObjects(env.repository, env.bucket);
  await env.repository.setExclusion('fb_100', false);
  await syncGallery(env);
  assert.equal(env.bucket.objects.size, 1);
  assert.equal((await env.repository.getPhoto('fb_100')).status, 'published');
});

test('photos without a Facebook ID keep one identity when excluded locally and restored', async t => {
  const env = await fixture(t);
  delete env.manifest.posts[0].assets[0].sourcePhotoId;
  await writeFile(join(env.root, 'data/manifest.json'), JSON.stringify(env.manifest));
  await syncGallery(env);
  const original = await env.repository.findPhoto('post', 'photo0');
  assert.equal(original.key.startsWith('sha_'), true);
  env.manifest.posts[0].assets[0].userExcluded = true;
  await writeFile(join(env.root, 'data/manifest.json'), JSON.stringify(env.manifest));
  await syncGallery(env);
  assert.equal((await env.repository.listPhotos()).filter(p => p.members.includes('post:photo0')).length, 1);
  assert.equal((await env.repository.getPhoto(original.key)).userExcluded, true);
  assert.equal(env.bucket.objects.size, 0);
});

test('a stale local exclusion cannot override a newer administrator restoration', async t => {
  const env = await fixture(t);
  await syncGallery(env);
  await env.repository.setExclusion('fb_100', true);
  env.manifest.posts[0].assets[0].userExcluded = true;
  await writeFile(join(env.root, 'data/manifest.json'), JSON.stringify(env.manifest));
  await writeFile(join(env.root, 'data/firebase-feedback-state.json'), JSON.stringify({ fb_100: 1 }));
  await env.repository.setExclusion('fb_100', false);
  await syncGallery(env);
  assert.equal((await env.repository.getPhoto('fb_100')).userExcluded, false);
  assert.equal((await env.repository.getPhoto('fb_100')).revision, 2);
});
