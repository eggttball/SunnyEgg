import { spawn } from 'node:child_process';
import { constants } from 'node:fs';
import { access, mkdir, readFile, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { resolve } from 'node:path';

const TIME_ZONE = 'Asia/Taipei';
const MAX_REPORT_BYTES = 8 * 1024 * 1024;
const COUNT_FIELDS = [
  'discoveredPosts', 'processedPosts', 'skippedPosts', 'duplicatePosts', 'failedPosts', 'cachedPosts',
  'inspectedPhotos', 'candidatePhotos', 'noMatchPhotos', 'noFacePhotos', 'skippedVideos',
  'likedPhotos', 'likedPosts', 'outOfRangePosts', 'missingDatePosts',
  'unprocessedObservations',
];
const TERMINAL_STATES = new Set(['complete', 'partial', 'blocked', 'failed']);
const PROGRESS_PHASES = new Set(['preparing', 'opening_post', 'scanning', 'resolving', 'downloading', 'recognizing', 'saving', 'finalizing']);
const MESSAGES = {
  idle: '尚未啟動手動抓取。',
  running: '正在抓取照片並進行本機比對，請稍候。',
  complete: '手動照片檢查完成；候選照片需要家長確認。',
  partial: '已完成部分照片檢查，仍有資料需要處理。',
  blocked: '手動照片工作尚未完成，請確認本機設定與登入。',
  failed: '手動照片工作執行失敗，請確認本機設定後重試。',
  batch_locked: '已有照片工作執行中，本次未啟動。',
  login_required: '專用瀏覽器登入已失效，請在本機重新登入。',
  scan_limit: '已達掃描上限；已處理觀察到的照片，所選日期範圍尚未完整。',
  feed_stalled: '貼文列表停止載入；已處理觀察到的照片，所選日期範圍尚未完整。',
  page_unavailable: 'Facebook 專頁目前無法讀取，請在本機確認專頁是否可開啟。',
  feed_unavailable: 'Facebook 貼文列表目前無法讀取，請稍後重試。',
  missing_date: '部分貼文缺少可確認的日期，已略過；所選日期範圍尚未完整。',
  invalid_date_range: '日期範圍無效，請重新選擇可用日期。',
  configuration: '請確認本機 Facebook 設定與照片工作設定。',
  recognition_prerequisites: '本機參考照片或辨識模型尚未就緒，請先完成設定。',
  browser_unavailable: '專用瀏覽器尚未就緒，請確認本機瀏覽器設定。',
  browser_dependency_missing: '尚未安裝 Playwright，請在 SunnyEgg 終端機執行 npm install 後重試。',
  browser_missing: '尚未安裝 Chromium，請在 SunnyEgg 終端機執行 npx playwright install chromium 後重試。',
  profile_missing: '尚未建立專用 Facebook 登入，請先執行專用瀏覽器登入指令並完成登入。',
  profile_unavailable: '無法存取專用瀏覽器設定檔，請確認本機資料目錄與檔案權限。',
  profile_in_use: '專用瀏覽器設定檔正在使用中，請先關閉整個專用瀏覽器後重試。',
  browser_launch_failed: '專用瀏覽器啟動失敗，請確認本機瀏覽器安裝後重試。',
  facebook_navigation_failed: '專用瀏覽器已開啟，但 Facebook 連線失敗，請確認網路連線後重試。',
  login_cancelled: '專用瀏覽器登入已取消，請重新執行登入指令並完成 Facebook 登入。',
  invalid_arguments: '專用瀏覽器指令參數無效，請確認照片工作設定後重試。',
  permission: 'Facebook 權限不足，請確認本機設定。',
  credentials: 'Facebook API 存取權杖已失效，請在本機更新 FB_ACCESS_TOKEN。',
  rate_limit: 'Facebook API 已達使用量限制，已暫停本次抓取；請稍後再試，已完成的照片結果會保留。',
  feedback_unavailable: '本機排除紀錄或負例照片無法讀取，請檢查本機資料後再試。',
  feedback_changed: '排除標記已變更，已暫停本次比對；請重新抓取以使用最新標記。',
  remote_failure: 'Facebook 讀取失敗，請稍後重試。',
  local_storage: '本機照片資料儲存失敗，請確認可用空間與權限。',
  reaction_failure: '候選照片已保存，但照片按讚尚未確認成功。',
  invalid_post_url: '請貼上小鹿托嬰中心 Facebook 的貼文網址。',
  post_unavailable: '指定貼文無法載入，或頁面尚未登入可查看。',
  post_no_photos: '指定貼文沒有可辨識的照片。',
};

export class ManualFetchError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.statusCode = statusCode;
  }
}

function validDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(value + 'T00:00:00.000Z');
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function subtractDays(value, days) {
  const date = new Date(value + 'T00:00:00.000Z');
  date.setUTCDate(date.getUTCDate() - days);
  return date.toISOString().slice(0, 10);
}

function subtractMonths(value, months) {
  const date = new Date(value + 'T00:00:00.000Z');
  const day = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() - months);
  const lastDay = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(day, lastDay));
  return date.toISOString().slice(0, 10);
}

export function createOptions(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
  const dateParts = Object.fromEntries(parts.map(part => [part.type, part.value]));
  const today = `${dateParts.year}-${dateParts.month}-${dateParts.day}`;
  const presets = [
    { value: '3d', label: '近 3 天', startDate: subtractDays(today, 2) },
    { value: '7d', label: '近 7 天', startDate: subtractDays(today, 6) },
    { value: '14d', label: '近 14 天', startDate: subtractDays(today, 13) },
    { value: '1m', label: '近 1 個月', startDate: subtractMonths(today, 1) },
    { value: '3m', label: '近 3 個月', startDate: subtractMonths(today, 3) },
  ].map(preset => ({ ...preset, endDate: today }));
  return {
    timeZone: TIME_ZONE, today, minDate: subtractMonths(today, 3), maxDate: today,
    defaultRange: { startDate: presets[0].startDate, endDate: today }, presets,
  };
}

export function validateRange(input, now = new Date()) {
  const { minDate, maxDate } = createOptions(now);
  if (!input || typeof input !== 'object' || Array.isArray(input) ||
      Object.keys(input).some(key => !['startDate', 'endDate'].includes(key)) ||
      !validDate(input.startDate) || !validDate(input.endDate) ||
      input.startDate > input.endDate || input.startDate < minDate || input.endDate > maxDate) {
    throw new ManualFetchError(400, '請選擇有效日期，範圍須介於近三個月與今天之間。');
  }
  return { startDate: input.startDate, endDate: input.endDate, timeZone: TIME_ZONE };
}

export function normalizePostUrl(value) {
  if (typeof value !== 'string' || value.length > 4096 || value.trim() !== value) {
    throw new ManualFetchError(400, MESSAGES.invalid_post_url);
  }
  let url;
  try { url = new URL(value); }
  catch { throw new ManualFetchError(400, MESSAGES.invalid_post_url); }
  if (url.protocol !== 'https:' || !['facebook.com', 'www.facebook.com', 'm.facebook.com'].includes(url.hostname) ||
      url.username || url.password || url.port || url.hash ||
      [...url.searchParams.keys()].some(key => /^(?:access_token|appsecret_proof|oauth_token|token|authorization)$/i.test(key))) {
    throw new ManualFetchError(400, MESSAGES.invalid_post_url);
  }
  if (['/permalink.php', '/story.php'].includes(url.pathname)) {
    const storyIds = url.searchParams.getAll('story_fbid'), pageIds = url.searchParams.getAll('id');
    if (storyIds.length !== 1 || pageIds.length !== 1 || !/^[A-Za-z0-9_.:-]{1,200}$/.test(storyIds[0]) || !/^\d+$/.test(pageIds[0])) {
      throw new ManualFetchError(400, MESSAGES.invalid_post_url);
    }
    return `https://www.facebook.com${url.pathname}?${new URLSearchParams({ story_fbid: storyIds[0], id: pageIds[0] })}`;
  }
  if (/^\/(?:[^/]+\/)?posts\/[A-Za-z0-9_.:-]+\/?$/.test(url.pathname)) {
    return `https://www.facebook.com${url.pathname.replace(/\/$/, '')}`;
  }
  if (/^\/share\/p\/[A-Za-z0-9_.:-]+\/?$/.test(url.pathname)) {
    return `https://www.facebook.com${url.pathname.replace(/\/$/, '')}`;
  }
  throw new ManualFetchError(400, MESSAGES.invalid_post_url);
}

function safeResult(report) {
  if (!report || typeof report !== 'object' || !TERMINAL_STATES.has(report.state)) throw new Error('Invalid job result');
  const counts = {};
  for (const key of COUNT_FIELDS) {
    const value = report.counts?.[key];
    if (Number.isSafeInteger(value) && value >= 0) counts[key] = value;
  }
  const message = Object.hasOwn(MESSAGES, report.reason) ? MESSAGES[report.reason] : MESSAGES[report.state];
  return { state: report.state, message, ...(Object.keys(counts).length ? { counts } : {}) };
}

function safeProgress(value) {
  if (!value || typeof value !== 'object' || !PROGRESS_PHASES.has(value.phase)) return null;
  const progress = { phase: value.phase };
  for (const key of ['scrolls', 'scrollLimit', 'discoveredPosts', 'completedPosts', 'totalPosts',
    'currentPost', 'currentPhoto', 'currentPhotoTotal']) {
    const number = value[key];
    if (Number.isSafeInteger(number) && number >= 0) progress[key] = number;
  }
  return progress;
}

export function createManualFetchController({ root, runner = createManualFetchRunner({ root }), clock = () => new Date() }) {
  let status = { state: 'idle', message: MESSAGES.idle };
  const getStatus = () => structuredClone(status);
  const launch = (now, { range, postUrl }) => {
    const accepted = { state: 'running', ...(range ? { range } : { targetType: 'post' }),
      startedAt: now.toISOString(), message: MESSAGES.running,
      runId: 'manual-' + randomUUID(), progress: { phase: 'preparing' } };
    status = accepted;
    const runnerInput = { ...(range || { postUrl }), runId: accepted.runId };
    Object.defineProperty(runnerInput, 'onProgress', { value: value => {
      const progress = safeProgress(value);
      if (progress && status.state === 'running' && status.runId === accepted.runId) status = { ...status, progress };
    } });
    Promise.resolve().then(() => runner(runnerInput)).then(report => {
      const { progress, ...terminal } = accepted;
      status = { ...terminal, ...safeResult(report), finishedAt: clock().toISOString() };
    }).catch(() => {
      const { progress, ...terminal } = accepted;
      status = { ...terminal, state: 'failed', message: MESSAGES.failed, finishedAt: clock().toISOString() };
    });
    return getStatus();
  };
  return {
    getOptions: () => createOptions(clock()),
    getStatus,
    start(input) {
      if (status.state === 'running') throw new ManualFetchError(409, '已有手動照片工作執行中，請等待完成。');
      const now = clock();
      return launch(now, { range: validateRange(input, now), postUrl: null });
    },
    startPost(input) {
      if (status.state === 'running') throw new ManualFetchError(409, '已有手動照片工作執行中，請等待完成。');
      if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).some(key => key !== 'url')) {
        throw new ManualFetchError(400, MESSAGES.invalid_post_url);
      }
      return launch(clock(), { range: null, postUrl: normalizePostUrl(input.url) });
    },
  };
}

export function createManualFetchRunner({ root }) {
  const projectRoot = resolve(root);
  return async ({ startDate, endDate, postUrl, onProgress }) => {
    if (postUrl !== undefined) postUrl = normalizePostUrl(postUrl);
    else if (!validDate(startDate) || !validDate(endDate) || startDate > endDate) throw new Error('Invalid date range');
    const venvPython = resolve(projectRoot, '.venv/bin/python');
    const executable = await access(venvPython, constants.X_OK).then(() => venvPython, () => 'python3');
    const progressEnabled = typeof onProgress === 'function';
    const progressId = progressEnabled ? randomUUID() : null;
    const progressDirectory = progressEnabled ? resolve(projectRoot, 'data', 'runs') : null;
    const progressFile = progressEnabled ? resolve(progressDirectory, `.manual-progress-${progressId}.json`) : null;
    if (progressEnabled) await mkdir(progressDirectory, { recursive: true, mode: 0o700 });
    const args = [
      resolve(projectRoot, 'scripts/daily.py'), '--root', projectRoot,
      '--env-file', resolve(projectRoot, '.env'),
    ];
    if (postUrl !== undefined) args.push('--post-url', postUrl);
    else args.push('--start-date', startDate, '--end-date', endDate, '--max-scrolls', '100');
    if (progressId) args.push('--progress-id', progressId);
    return new Promise((resolveReport, reject) => {
      const child = spawn(executable, args, { cwd: projectRoot, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
      const chunks = [];
      let bytes = 0;
      let overflow = false;
      let readingProgress = false;
      const readProgress = async () => {
        if (readingProgress) return;
        readingProgress = true;
        try {
          const value = JSON.parse(await readFile(progressFile, 'utf8'));
          const progress = safeProgress(value);
          if (progress) onProgress(progress);
        } catch { /* Progress is best effort; the photo job remains authoritative. */ }
        finally { readingProgress = false; }
      };
      const progressTimer = progressEnabled ? setInterval(() => { void readProgress(); }, 500) : null;
      progressTimer?.unref?.();
      const cleanup = async () => {
        if (progressTimer) clearInterval(progressTimer);
        if (!progressEnabled) return;
        await readProgress();
        await unlink(progressFile).catch(() => {});
      };
      child.stdout.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > MAX_REPORT_BYTES) {
          overflow = true;
          child.kill();
        } else chunks.push(chunk);
      });
      child.stderr.on('data', () => {});
      child.on('error', async () => {
        await cleanup();
        reject(new Error('Photo job could not start'));
      });
      child.on('close', async (code, signal) => {
        await cleanup();
        try {
          if (overflow || signal) throw new Error();
          const report = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (!report || !TERMINAL_STATES.has(report.state) || (code !== 0 && report.state === 'complete')) throw new Error();
          resolveReport(report);
        } catch {
          reject(new Error('Photo job did not return a valid report'));
        }
      });
    });
  };
}
