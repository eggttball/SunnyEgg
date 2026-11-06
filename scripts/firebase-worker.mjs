import { readFile, writeFile, rename, mkdir, stat } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createOptions } from '../cloud/validation.mjs';
import { sourcePhotoId } from '../cloud/gallery.mjs';
import { createPhotoFeedbackRunner } from './photo-feedback.mjs';
import { createManualFetchRunner, safeResult as safeFetchResult } from './manual-fetch.mjs';
import { createManualPhotoRunner, safeResult as safePhotoResult } from './manual-photo.mjs';
import { syncGallery, deleteExcludedObjects } from './firebase-sync.mjs';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
export async function applyRemoteFeedback({ root, repository, applyFeedback = createPhotoFeedbackRunner({ root }) }) {
  const statePath = resolve(root, 'data/firebase-feedback-state.json');
  let state;
  try { state = JSON.parse(await readFile(statePath, 'utf8')); } catch (error) { if (error.code !== 'ENOENT') throw error; state = {}; }
  const version = await repository.feedbackVersion();
  if (state.__version === version) return false;
  const manifest = JSON.parse(await readFile(resolve(root, 'data/manifest.json'), 'utf8'));
  let complete = true;
  for (const photo of await repository.listPhotos()) {
    if (!photo.revision || photo.revision <= (state[photo.key] || 0)) continue;
    const matches = manifest.posts.flatMap(post => post.kind !== 'photos' ? [] : post.assets.filter(asset =>
      photo.sourcePhotoId ? sourcePhotoId(post, asset) === photo.sourcePhotoId : photo.members.includes(`${post.id}:${asset.id}`)).map(asset => ({ postId: post.id, assetId: asset.id })));
    if (!matches.length) { complete = false; continue; }
    await repository.assertOwnership();
    await applyFeedback({ ...matches[0], excluded: photo.userExcluded === true });
    state[photo.key] = photo.revision;
  }
  if (complete) state.__version = version;
  const temporary = `${statePath}.${randomUUID()}.tmp`;
  await writeFile(temporary, JSON.stringify(state) + '\n', { mode: 0o600, flag: 'wx' });
  await rename(temporary, statePath);
  return true;
}

export async function queueDailyJob(repository, now = new Date()) {
  const hour = Number(new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Taipei', hour: '2-digit', hourCycle: 'h23' }).format(now));
  if (hour < 20) return false;
  const options = createOptions(now);
  try { await repository.enqueueJob('fetch', { ...options.defaultRange, timeZone: options.timeZone }, { id: `daily-${options.today}`, awaitingApproval: true }); return true; }
  catch (error) { if (error.statusCode === 409) return false; throw error; }
}

export async function processCloudJob({ job, owner, repository, root, bucket,
  fetchRunner = createManualFetchRunner({ root }), photoRunner = createManualPhotoRunner({ root }),
  reconcile = applyRemoteFeedback, sync = syncGallery, applyFeedback, signal }) {
  let progressWrites = Promise.resolve();
  const scoped = repository.forWorker(owner, job.id);
  try {
    if (!await repository.updateJob(job.id, owner, {})) return;
    await scoped.assertOwnership();
    await reconcile({ root, repository: scoped, applyFeedback });
    let report;
    if (job.kind === 'photo') report = safePhotoResult(await photoRunner({ ...job.input, signal }));
    else report = safeFetchResult(await fetchRunner({ ...job.input, signal, onProgress: value => {
      const progress = {};
      for (const key of ['phase', 'scrolls', 'scrollLimit', 'discoveredPosts', 'completedPosts', 'totalPosts', 'currentPost', 'currentPhoto', 'currentPhotoTotal']) {
        if (key === 'phase' ? ['preparing', 'opening_post', 'scanning', 'resolving', 'collecting', 'downloading', 'recognizing', 'saving', 'finalizing'].includes(value[key]) : Number.isSafeInteger(value[key]) && value[key] >= 0) progress[key] = value[key];
      }
      progressWrites = progressWrites.then(() => repository.updateJob(job.id, owner, { progress })).catch(() => {});
    } }));
    await progressWrites;
    if (!await repository.updateJob(job.id, owner, {})) return;
    await scoped.assertOwnership();
    await reconcile({ root, repository: scoped, applyFeedback });
    await sync({ root, repository: scoped, bucket });
    await repository.updateJob(job.id, owner, { ...report, progress: { phase: 'finalizing' } });
  } catch {
    await repository.updateJob(job.id, owner, { state: 'failed', message: '照片處理或 Firebase 同步尚未完成，請檢查本機設定後重試。' });
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.some(value => !['--sync', '--once', '--schedule-only'].includes(value))) throw new Error('可使用 --sync 或 --once。');
  const { firebaseRuntime } = await import('./firebase-runtime.mjs');
  const { repository, bucket } = await firebaseRuntime(ROOT, { media: !args.includes('--schedule-only') });
  if (args.includes('--schedule-only')) { await queueDailyJob(repository); console.log('每日工作已建立，等待管理員同意。'); return; }
  const root = ROOT, owner = randomUUID();
  const scoped = repository.forWorker(owner);
  await mkdir(resolve(root, 'data'), { recursive: true, mode: 0o700 });
  const unlockMedia = await bucket.acquireLock();
  try { if (!await repository.heartbeat(owner)) throw new Error('已有 Firebase worker 執行中。'); }
  catch (error) { await unlockMedia(); throw error; }
  const workerAbort = new AbortController(); bucket.signal = workerAbort.signal; bucket.setRepository(scoped);
  let runningJob = null, leaseLost = false, controller = null;
  const loseLease = () => { leaseLost = true; workerAbort.abort(); controller?.abort(); };
  const heartbeat = setInterval(async () => {
    try {
      if (!await repository.heartbeat(owner)) loseLease();
      if (runningJob && !await repository.updateJob(runningJob.id, owner, {})) loseLease();
    } catch { loseLease(); }
  }, 30000);
  let stop = false, lastManifest = null, lastCleanup = 0;
  const stopWorker = () => { stop = true; workerAbort.abort(); controller?.abort(); };
  process.on('SIGINT', stopWorker); process.on('SIGTERM', stopWorker);
  try {
    if (args.includes('--sync')) {
      await applyRemoteFeedback({ root, repository: scoped });
      const counts = await syncGallery({ root, repository: scoped, bucket });
      console.log(JSON.stringify({ state: 'complete', ...counts })); return;
    }
    console.log('SunnyEgg Firebase worker 已啟動；每天台灣時間 20:00 建立待同意工作；管理員同意後才抓取。');
    do {
      if (leaseLost) throw new Error('Firebase worker 連線或工作租約已失效，請重新啟動。');
      try {
        const changed = await applyRemoteFeedback({ root, repository: scoped });
        const modified = (await stat(resolve(root, 'data/manifest.json'))).mtimeMs;
        if (changed || modified !== lastManifest) { await syncGallery({ root, repository: scoped, bucket }); lastManifest = (await stat(resolve(root, 'data/manifest.json'))).mtimeMs; }
        if (Date.now() - lastCleanup > 300000) { const removed = await deleteExcludedObjects(scoped, bucket); if (removed) await bucket.commit(scoped); lastCleanup = Date.now(); }
        await queueDailyJob(repository);
        runningJob = await repository.claimJob(owner);
        if (runningJob) {
          controller = new AbortController();
          await processCloudJob({ root, repository, bucket, owner, job: runningJob, signal: controller.signal });
          controller = null; bucket.setRepository(scoped);
          runningJob = null; lastManifest = null;
        }
      } catch { console.error('Firebase worker 本次連線或同步未完成，將稍後重試。'); }
      if (!stop && !args.includes('--once')) await new Promise(resolveWait => setTimeout(resolveWait, 15000));
    } while (!stop && !args.includes('--once'));
  } finally {
    clearInterval(heartbeat); process.off('SIGINT', stopWorker); process.off('SIGTERM', stopWorker);
    await repository.releaseWorker(owner).catch(() => {});
    await unlockMedia();
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => { console.error('Firebase worker 尚未完成，請確認服務帳戶、本機資料與網路設定。'); process.exitCode = 1; });
}
