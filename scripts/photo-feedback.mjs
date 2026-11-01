import { spawn } from 'node:child_process';
import { constants } from 'node:fs';
import { access } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const MESSAGES = {
  400: '請提供有效的照片識別碼與排除狀態。',
  404: '找不到指定的本機照片。',
  409: '照片原始檔無法驗證，尚未保存標記。',
  503: '照片標記尚未確認保存，請重新讀取後重試。',
};

export class PhotoFeedbackError extends Error {
  constructor(statusCode = 503) {
    super(MESSAGES[statusCode] || MESSAGES[503]);
    this.statusCode = Object.hasOwn(MESSAGES, statusCode) ? statusCode : 503;
  }
}

export function validatePhotoFeedback(input) {
  const validId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(value);
  if (!input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).sort().join(',') !== 'assetId,excluded,postId'
      || !validId(input.postId) || !validId(input.assetId) || typeof input.excluded !== 'boolean') {
    throw new PhotoFeedbackError(400);
  }
  return { postId: input.postId, assetId: input.assetId, excluded: input.excluded };
}

export function safePhotoFeedbackResult(result, input) {
  if (!result || result.postId !== input.postId || result.assetId !== input.assetId || result.excluded !== input.excluded) {
    throw new PhotoFeedbackError();
  }
  const value = { ...input };
  for (const key of ['negativeExampleCount', 'revision']) {
    if (Number.isSafeInteger(result[key]) && result[key] >= 0) value[key] = result[key];
  }
  return value;
}

export function createPhotoFeedbackRunner({ root }) {
  const projectRoot = resolve(root);
  const script = fileURLToPath(new URL('./feedback.py', import.meta.url));
  return async input => {
    const value = validatePhotoFeedback(input);
    const venvPython = resolve(projectRoot, '.venv/bin/python');
    const executable = await access(venvPython, constants.X_OK).then(() => venvPython, () => 'python3');
    const args = [script, '--root', projectRoot, '--post-id', value.postId, '--asset-id', value.assetId, '--excluded', value.excluded ? '1' : '0'];
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => ['PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT', 'WINDIR'].includes(key)));
    return new Promise((resolveResult, reject) => {
      const child = spawn(executable, args, { cwd: projectRoot, env, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
      const chunks = [];
      let size = 0, invalid = false;
      const timer = setTimeout(() => { invalid = true; child.kill(); }, 30000);
      child.stdout.on('data', chunk => {
        size += chunk.length;
        if (size > 65536) { invalid = true; child.kill(); }
        else chunks.push(chunk);
      });
      child.stderr.on('data', () => {});
      child.on('error', () => { clearTimeout(timer); reject(new PhotoFeedbackError()); });
      child.on('close', (code, signal) => {
        clearTimeout(timer);
        try {
          if (invalid || signal) throw new PhotoFeedbackError();
          const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (code !== 0) throw new PhotoFeedbackError(result?.statusCode);
          resolveResult(safePhotoFeedbackResult(result, value));
        } catch (error) { reject(error instanceof PhotoFeedbackError ? error : new PhotoFeedbackError()); }
      });
    });
  };
}
