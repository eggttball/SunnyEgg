import { spawn } from 'node:child_process';
import { attachAbort } from './process-abort.mjs';
import { constants } from 'node:fs';
import { access } from 'node:fs/promises';
import { resolve } from 'node:path';

const MAX_RESULT_BYTES = 1024 * 1024;
const PHOTO_ID = /^[0-9]+$/;
const SECRET_QUERY = /^(?:access_token|appsecret_proof|oauth_token|token|authorization)$/i;
const PHOTO_PATHS = new Set(['/photo', '/photo/', '/photo.php']);
const ACTIONS = new Set(['added', 'promoted', 'already_selected', 'excluded']);
const RUN_STATES = new Set(['complete', 'partial', 'blocked', 'failed']);
const REACTION_MESSAGES = {
  liked: '已替這張照片按讚。',
  already_liked: '這張照片已按讚。',
  not_requested: '自動按讚目前關閉。',
  failed: '照片已保存，但按讚未成功；請確認專用 Facebook 瀏覽器登入後，用同一個照片網址重試。',
};
const STORAGE_REACTION_MESSAGE = '照片已保存，但無法確認或記錄按讚狀態；請確認本機儲存空間與檔案權限後重試。';
const MESSAGES = {
  idle: '尚未手動加入照片。',
  running: '正在確認粉專來源、加入指定照片並處理按讚。',
  added: '照片已手動加入原貼文相簿。',
  promoted: '照片原已在相簿，已標記為手動加入。',
  already_selected: '照片已手動加入，沒有重複新增。',
  excluded: '照片目前標記為排除；沒有變更這項標記。',
  wrong_page: '此照片不屬於設定的小鹿托嬰中心粉專。',
  invalid_photo_url: '請貼上含有 fbid 的 Facebook 照片頁網址。',
  invalid_response: 'Facebook 未能確認這張照片的原貼文。',
  configuration: '請確認本機 Facebook 設定。',
  not_found: 'Facebook 找不到這張照片，請確認網址有效且照片仍可查看。',
  photo_unavailable: 'Facebook 目前無法提供這張照片的原圖。',
  download_failure: 'Facebook 原圖下載失敗，請稍後重試。',
  unsupported_media: 'Facebook 回傳的檔案不是支援的照片格式。',
  remote_failure: 'Facebook 讀取失敗，請稍後重試。',
  credentials: 'Facebook API 權杖無效或已過期，請在本機更新。',
  permission: 'Facebook API 權限不足，無法讀取這張照片。',
  rate_limit: 'Facebook API 暫時達到讀取上限，請稍後重試。',
  batch_locked: '已有照片工作執行中，請等待完成後再試。',
  local_storage: '本機無法安全保存照片，請確認資料夾權限與可用空間。',
  failed: '手動加入照片失敗，請稍後重試。',
};

export class ManualPhotoError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.statusCode = statusCode;
  }
}

export function photoIdFromUrl(value) {
  if (typeof value !== 'string' || value.length > 4096 || value.trim() !== value) {
    throw new ManualPhotoError(400, MESSAGES.invalid_photo_url);
  }
  let url;
  try { url = new URL(value); }
  catch { throw new ManualPhotoError(400, MESSAGES.invalid_photo_url); }
  if (url.protocol !== 'https:' ||
      !['facebook.com', 'www.facebook.com', 'm.facebook.com'].includes(url.hostname) ||
      url.username || url.password || url.port || url.hash ||
      !PHOTO_PATHS.has(url.pathname) ||
      [...url.searchParams.keys()].some(key => SECRET_QUERY.test(key))) {
    throw new ManualPhotoError(400, MESSAGES.invalid_photo_url);
  }
  const identifiers = url.searchParams.getAll('fbid');
  if (identifiers.length !== 1 || !PHOTO_ID.test(identifiers[0])) {
    throw new ManualPhotoError(400, MESSAGES.invalid_photo_url);
  }
  return identifiers[0];
}

export function safeResult(value) {
  if (!value || typeof value !== 'object' || !RUN_STATES.has(value.state)) throw new Error('Invalid photo import result');
  if (['complete', 'partial'].includes(value.state) && ACTIONS.has(value.action)) {
    const reaction = Object.hasOwn(REACTION_MESSAGES, value.reaction) ? value.reaction : null;
    if (value.state === 'partial' && reaction !== 'failed') throw new Error('Invalid partial photo import result');
    const reason = reaction === 'failed' ? (value.reason === 'local_storage' ? 'local_storage' : 'reaction_failure') : null;
    const reactionMessage = reason === 'local_storage' ? STORAGE_REACTION_MESSAGE : REACTION_MESSAGES[reaction];
    const message = MESSAGES[value.action] + (reaction && value.action !== 'excluded' ? ' ' + reactionMessage : '');
    return { state: reaction === 'failed' ? 'partial' : 'complete', action: value.action,
      ...(reaction ? { reaction } : {}), ...(reason ? { reason } : {}), message };
  }
  const reason = Object.hasOwn(MESSAGES, value.reason) ? value.reason : 'failed';
  return { state: value.state === 'blocked' ? 'blocked' : 'failed', message: MESSAGES[reason] };
}

export function createManualPhotoController({ root, runner = createManualPhotoRunner({ root }), clock = () => new Date() }) {
  let status = { state: 'idle', message: MESSAGES.idle };
  const getStatus = () => structuredClone(status);
  return {
    getStatus,
    start(input) {
      if (status.state === 'running') throw new ManualPhotoError(409, '已有手動照片工作執行中，請等待完成。');
      if (!input || typeof input !== 'object' || Array.isArray(input) ||
          Object.keys(input).some(key => key !== 'url')) {
        throw new ManualPhotoError(400, MESSAGES.invalid_photo_url);
      }
      const photoId = photoIdFromUrl(input.url);
      const accepted = { state: 'running', startedAt: clock().toISOString(), message: MESSAGES.running };
      status = accepted;
      Promise.resolve().then(() => runner({ photoId })).then(result => {
        status = { ...accepted, ...safeResult(result), finishedAt: clock().toISOString() };
      }).catch(() => {
        status = { ...accepted, state: 'failed', message: MESSAGES.failed, finishedAt: clock().toISOString() };
      });
      return getStatus();
    },
  };
}

export function createManualPhotoRunner({ root }) {
  const projectRoot = resolve(root);
  return async ({ photoId, signal }) => {
    if (typeof photoId !== 'string' || !PHOTO_ID.test(photoId)) throw new Error('Invalid photo ID');
    signal?.throwIfAborted();
    const venvPython = resolve(projectRoot, '.venv/bin/python');
    const executable = await access(venvPython, constants.X_OK).then(() => venvPython, () => 'python3');
    const args = [
      resolve(projectRoot, 'scripts/manual_photo.py'), '--root', projectRoot,
      '--env-file', resolve(projectRoot, '.env'), '--photo-id', photoId,
    ];
    return new Promise((resolveResult, reject) => {
      const child = spawn(executable, args, { cwd: projectRoot, shell: false, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] });
      const detachAbort = attachAbort(child, signal);
      const chunks = [];
      let bytes = 0;
      let overflow = false;
      child.stdout.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > MAX_RESULT_BYTES) {
          overflow = true;
          child.kill();
        } else chunks.push(chunk);
      });
      child.stderr.on('data', () => {});
      child.on('error', () => { detachAbort(); reject(new Error('Manual photo job could not start')); });
      child.on('close', (code, signal) => {
        detachAbort();
        try {
          if (overflow || signal) throw new Error();
          const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (!result || !RUN_STATES.has(result.state) || (code !== 0 && result.state === 'complete')) throw new Error();
          resolveResult(result);
        } catch {
          reject(new Error('Manual photo job did not return a valid result'));
        }
      });
    });
  };
}
