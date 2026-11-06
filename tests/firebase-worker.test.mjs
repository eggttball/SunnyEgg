import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { FirestoreRepository } from '../cloud/repository.mjs';
import { MemoryFirestore, MemoryBucket } from './firebase-fixture.mjs';
import { applyRemoteFeedback, queueDailyJob, processCloudJob } from '../scripts/firebase-worker.mjs';

test('daily jobs await approval at Taipei 20:00 and the same day is not queued twice after restart', async () => {
  const repository = new FirestoreRepository(new MemoryFirestore());
  assert.equal(await queueDailyJob(repository, new Date('2026-11-06T11:59:00Z')), false);
  assert.equal(await queueDailyJob(repository, new Date('2026-11-06T12:00:00Z')), true);
  assert.equal(await repository.claimJob('worker'), null);
  assert.equal((await repository.pendingApproval()).state, 'awaiting_approval');
  await repository.approveDailyJob('daily-2026-11-06');
  await repository.heartbeat('worker');
  const job = await repository.claimJob('worker');
  assert.equal(job.id, 'daily-2026-11-06');
  assert.equal(job.input.endDate, '2026-11-06');
  await repository.updateJob(job.id, 'worker', { state: 'complete' });
  await queueDailyJob(repository, new Date('2026-11-06T12:30:00Z'));
  assert.equal(await repository.claimJob('worker'), null);
});

test('cloud feedback uses photo identity and each revision is applied locally once', async t => {
  const root = await mkdtemp(join(tmpdir(), 'sunnyegg-cloud-feedback-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, 'data'));
  const manifest = { version: 1, posts: [{ id: 'local-post', kind: 'photos', assets: [{ id: 'local-asset', type: 'image', sourcePhotoId: '123' }] }] };
  await writeFile(join(root, 'data/manifest.json'), JSON.stringify(manifest));
  const repository = new FirestoreRepository(new MemoryFirestore());
  await repository.registerPhoto({ key: 'fb_123', sourcePhotoId: '123', postId: 'old-post', assetId: 'old-asset', members: ['old-post:old-asset'] });
  await repository.setExclusion('fb_123', true);
  const applied = [];
  const applyFeedback = async input => { applied.push(input); };
  await applyRemoteFeedback({ root, repository, applyFeedback });
  await applyRemoteFeedback({ root, repository, applyFeedback });
  assert.deepEqual(applied, [{ postId: 'local-post', assetId: 'local-asset', excluded: true }]);
  await repository.setExclusion('fb_123', false);
  await applyRemoteFeedback({ root, repository, applyFeedback });
  assert.deepEqual(applied[1], { postId: 'local-post', assetId: 'local-asset', excluded: false });
  assert.equal(JSON.parse(await readFile(join(root, 'data/firebase-feedback-state.json'))).fb_123, 2);
});

test('worker reports progress, publishes before completing the job and sanitizes failure messages', async () => {
  const repository = new FirestoreRepository(new MemoryFirestore());
  await repository.enqueueJob('post', { postUrl: 'https://www.facebook.com/100063963161862/posts/123' });
  await repository.heartbeat('worker');
  const job = await repository.claimJob('worker');
  let published = false;
  await processCloudJob({ job, owner: 'worker', repository, bucket: new MemoryBucket(), root: '/unused',
    applyFeedback: async () => {}, sync: async () => { published = true; },
    fetchRunner: async ({ onProgress }) => { onProgress({ phase: 'recognizing', currentPhoto: 2, currentPhotoTotal: 3 }); return { state: 'complete', counts: { candidatePhotos: 1 }, message: 'secret details' }; },
    reconcile: async () => {},
  });
  const status = await repository.jobStatus('fetch');
  assert.equal(published, true);
  assert.equal(status.state, 'complete');
  assert.equal(status.counts.candidatePhotos, 1);
  assert.equal(status.message.includes('secret details'), false);
});

test('a worker that loses its job lease cannot publish the old runner result', async () => {
  let now = 0;
  const repository = new FirestoreRepository(new MemoryFirestore(), { clock: () => now });
  await repository.enqueueJob('post', { postUrl: 'https://www.facebook.com/100063963161862/posts/123' });
  await repository.heartbeat('old');
  const job = await repository.claimJob('old');
  let published = false;
  await processCloudJob({ job, owner: 'old', repository, bucket: new MemoryBucket(), root: '/unused',
    reconcile: async () => {}, sync: async () => { published = true; },
    fetchRunner: async () => { now = 120001; await repository.heartbeat('new'); await repository.claimJob('new'); return { state: 'complete' }; },
  });
  assert.equal(published, false);
});

test('expired worker fencing rejects pruning a photo published by a replacement worker', async () => {
  let now = 1;
  const repository = new FirestoreRepository(new MemoryFirestore(), { clock: () => now });
  await repository.heartbeat('old');
  const old = repository.forWorker('old');
  await old.registerPhoto({ key: 'fb_123', postId: 'post', assetId: 'asset', members: ['post:asset'] });
  now = 120002;
  await repository.heartbeat('new');
  await assert.rejects(old.unpublish('fb_123'));
  assert.equal((await repository.getPhoto('fb_123')).status, 'pending');
});

 test('pending daily consent does not block a manual job and cannot approve while another job runs', async () => {
  const repository = new FirestoreRepository(new MemoryFirestore());
  await queueDailyJob(repository, new Date('2026-11-06T12:00:00Z'));
  await repository.enqueueJob('photo', { photoId: '123' });
  await assert.rejects(repository.approveDailyJob('daily-2026-11-06'));
  await repository.heartbeat('worker');
  const manual = await repository.claimJob('worker');
  assert.equal(manual.kind, 'photo');
  await repository.updateJob(manual.id, 'worker', { state: 'complete' });
  await repository.approveDailyJob('daily-2026-11-06');
  assert.equal((await repository.claimJob('worker')).kind, 'fetch');
 });
 test('a staged photo is invisible until its exact deployment succeeds and exclusion invalidates deployment', async () => {
  const repository = new FirestoreRepository(new MemoryFirestore(), { deferPublication: true });
  await repository.registerPhoto({ key: 'fb_123', postId: 'post', assetId: 'asset', members: ['post:asset'] });
  const reservation = await repository.beginPublication('fb_123', 'hash', 'photos/fb_123/image.jpg');
  const payload = { objectPath: 'photos/fb_123/image.jpg', imageHash: 'hash', contentType: 'image/jpeg', size: 4 };
  await repository.finishPublication('fb_123', reservation, payload);
  assert.equal((await repository.getPhoto('fb_123')).status, 'staged');
  assert.equal(await repository.completeDeployment('fb_123', reservation, payload), true);
  await repository.setExclusion('fb_123', true);
  assert.equal(await repository.completeDeployment('fb_123', reservation, payload), false);
 });

test('global worker takeover fences old status writes and recovers an unexpired old job', async () => {
  let now = 0;
  const repository = new FirestoreRepository(new MemoryFirestore(), { clock: () => now });
  await repository.heartbeat('old');
  await repository.enqueueJob('photo', { photoId: '123' }, { id: 'handover' });
  const job = await repository.claimJob('old');
  now = 10000;
  await repository.updateJob(job.id, 'old', {});
  now = 120001;
  await repository.heartbeat('new');
  assert.equal(await repository.updateJob(job.id, 'old', { state: 'failed' }), false);
  assert.equal((await repository.claimJob('new')).owner, 'new');
});
