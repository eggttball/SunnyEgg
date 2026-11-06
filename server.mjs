import http from 'node:http';
import { createReadStream } from 'node:fs';
import { readFile, readdir, realpath, stat } from 'node:fs/promises';
import { dirname, extname, resolve, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createManualFetchController, ManualFetchError } from './scripts/manual-fetch.mjs';
import { createManualPhotoController, ManualPhotoError } from './scripts/manual-photo.mjs';
import { createPhotoFeedbackRunner, PhotoFeedbackError, safePhotoFeedbackResult, validatePhotoFeedback } from './scripts/photo-feedback.mjs';

const PROJECT_ROOT = dirname(fileURLToPath(import.meta.url));
const REFERENCE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.webp'];
const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
  '.gif': 'image/gif', '.mp4': 'video/mp4', '.webm': 'video/webm',
  '.mov': 'video/quicktime', '.m4v': 'video/mp4',
};

export function parseRange(header, size) {
  if (!Number.isSafeInteger(size) || size <= 0) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header);
  if (!match || (!match[1] && !match[2])) return null;
  let start, end;
  if (!match[1]) {
    const suffix = Number(match[2]);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return null;
    start = Math.max(0, size - suffix);
    end = size - 1;
  } else {
    start = Number(match[1]);
    end = match[2] ? Number(match[2]) : size - 1;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start > end || start >= size) return null;
    end = Math.min(end, size - 1);
  }
  return { start, end };
}

function json(res, code, value) {
  const body = JSON.stringify(value);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}

async function readJsonBody(req, invalidMessage = '請提供有效的日期範圍。') {
  const limit = 8192;
  if (Number(req.headers['content-length']) > limit) {
    req.resume();
    throw new ManualFetchError(413, '請求內容過大。');
  }
  const chunks = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > limit) throw new ManualFetchError(413, '請求內容過大。');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new ManualFetchError(400, invalidMessage); }
}

async function isolatedFile(base, requested) {
  const target = resolve(base, requested);
  const inside = relative(base, target);
  if (!inside || inside.startsWith(`..${sep}`) || inside === '..' || inside.startsWith(sep)) return null;
  const canonical = await realpath(target);
  const canonicalRelative = relative(base, canonical);
  if (!canonicalRelative || canonicalRelative === '..' || canonicalRelative.startsWith(`..${sep}`) || canonicalRelative.startsWith(sep)) return null;
  const info = await stat(canonical);
  return info.isFile() ? { path: canonical, size: info.size } : null;
}

async function gallery(root) {
  const data = JSON.parse(await readFile(resolve(root, 'data/manifest.json'), 'utf8'));
  if (data.version !== 1 || !Array.isArray(data.posts)) throw new Error('Invalid media manifest');
  const files = await readdir(resolve(root, 'data/reference-child'), { withFileTypes: true }).catch(error => {
    if (error.code === 'ENOENT') return [];
    throw error;
  });
  return {
    ...data,
    posts: data.posts.filter(post => post && typeof post === 'object' && post.kind === 'photos').map(post => {
      const { clips, playback, embedHtml, ...photoPost } = post;
      const assets = Array.isArray(post.assets) ? post.assets.filter(asset => asset && asset.type === 'image') : [];
      const imageIds = new Set(assets.map(asset => asset.id));
      const recognition = post.recognition && typeof post.recognition === 'object' ? {
        ...post.recognition,
        results: Array.isArray(post.recognition.results) ? post.recognition.results.filter(result => result && imageIds.has(result.assetId)) : [],
      } : undefined;
      return { ...photoPost, assets, ...(recognition ? { recognition } : {}) };
    }),
    reference: {
      count: files.filter(file => file.isFile() && !file.name.startsWith('.') && REFERENCE_EXTENSIONS.includes(extname(file.name).toLowerCase())).length,
      directory: 'data/reference-child',
      supportedExtensions: REFERENCE_EXTENSIONS,
    },
  };
}

export function createGalleryHandler({ root = PROJECT_ROOT, manualFetchRunner, photoFeedbackRunner = createPhotoFeedbackRunner({ root }), clock } = {}) {
  const manualFetch = createManualFetchController({ root, runner: manualFetchRunner, clock });
  const manualPhoto = createManualPhotoController({ root, clock });
  return async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; media-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");
    if (!/^(127\.0\.0\.1|localhost)(:\d+)?$/.test(req.headers.host || '')) {
      return json(res, 403, { error: '只接受本機連線' });
    }
    let pathname;
    try {
      pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
      const requestUrl = new URL(req.url, 'http://localhost');
      if (pathname === '/api/photo-feedback') {
        if (req.method !== 'POST') return json(res, 405, { error: '不支援此操作' });
        if (req.headers.origin !== `http://${req.headers.host}` ||
            (req.headers['sec-fetch-site'] && !['same-origin', 'none'].includes(req.headers['sec-fetch-site']))) {
          return json(res, 403, { error: '只接受同來源的本機請求。' });
        }
        if (!/^application\/json(?:\s*;|\s*$)/i.test(req.headers['content-type'] || '')) {
          return json(res, 415, { error: '請使用 JSON 照片標記資料。' });
        }
        const input = validatePhotoFeedback(await readJsonBody(req, '請提供有效的照片標記資料。'));
        return json(res, 200, safePhotoFeedbackResult(await photoFeedbackRunner(input), input));
      }
      if (pathname === '/api/manual-fetch' && req.method === 'POST') {
        if (req.headers.origin !== `http://${req.headers.host}` ||
            (req.headers['sec-fetch-site'] && !['same-origin', 'none'].includes(req.headers['sec-fetch-site']))) {
          return json(res, 403, { error: '只接受同來源的本機請求。' });
        }
        if (!/^application\/json(?:\s*;|\s*$)/i.test(req.headers['content-type'] || '')) {
          return json(res, 415, { error: '請使用 JSON 日期資料。' });
        }
        const input = await readJsonBody(req);
        return json(res, 202, manualFetch.start(input));
      }
      if (pathname === '/api/manual-fetch/post' && req.method === 'POST') {
        if (requestUrl.searchParams.getAll('ctrl').length !== 1 || requestUrl.searchParams.get('ctrl') !== '1') {
          return json(res, 404, { error: '找不到內容' });
        }
        if (req.headers.origin !== `http://${req.headers.host}` ||
            (req.headers['sec-fetch-site'] && !['same-origin', 'none'].includes(req.headers['sec-fetch-site']))) {
          return json(res, 403, { error: '只接受同來源的本機請求。' });
        }
        if (!/^application\/json(?:\s*;|\s*$)/i.test(req.headers['content-type'] || '')) {
          return json(res, 415, { error: '請使用 JSON 貼文網址資料。' });
        }
        return json(res, 202, manualFetch.startPost(await readJsonBody(req, '請提供有效的 Facebook 貼文網址。')));
      }
      if (['/api/manual-photo', '/api/manual-photo/status'].includes(pathname)) {
        if (requestUrl.searchParams.getAll('ctrl').length !== 1 || requestUrl.searchParams.get('ctrl') !== '1') {
          return json(res, 404, { error: '找不到內容' });
        }
        if (pathname === '/api/manual-photo/status' || req.method === 'GET' || req.method === 'HEAD') {
          if (!['GET', 'HEAD'].includes(req.method)) return json(res, 405, { error: '不支援此操作' });
          if (req.method === 'HEAD') { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); return res.end(); }
          return json(res, 200, manualPhoto.getStatus());
        }
        if (req.method === 'POST') {
          if (req.headers.origin !== `http://${req.headers.host}` ||
              (req.headers['sec-fetch-site'] && !['same-origin', 'none'].includes(req.headers['sec-fetch-site']))) {
            return json(res, 403, { error: '只接受同來源的本機請求。' });
          }
          if (!/^application\/json(?:\s*;|\s*$)/i.test(req.headers['content-type'] || '')) {
            return json(res, 415, { error: '請使用 JSON 照片資料。' });
          }
          return json(res, 202, manualPhoto.start(await readJsonBody(req, '請提供有效的 Facebook 照片頁網址。')));
        }
        return json(res, 405, { error: '不支援此操作' });
      }
      if (!['GET', 'HEAD'].includes(req.method)) return json(res, 405, { error: '不支援此操作' });
      if (['/api/manual-fetch/options', '/api/manual-fetch/status'].includes(pathname)) {
        const data = pathname.endsWith('/options') ? manualFetch.getOptions() : manualFetch.getStatus();
        if (req.method === 'HEAD') { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); return res.end(); }
        return json(res, 200, data);
      }
      const canonicalRoot = await realpath(root);
      if (pathname === '/api/gallery') {
        const data = await gallery(canonicalRoot);
        if (req.method === 'HEAD') { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); return res.end(); }
        return json(res, 200, data);
      }
      let base, requested;
      const frontend = { '/': 'index.html', '/index.html': 'index.html', '/styles.css': 'styles.css', '/admin-auth.css': 'admin-auth.css', '/app.js': 'app.js' };
      if (Object.hasOwn(frontend, pathname)) {
        base = resolve(canonicalRoot, 'public'); requested = frontend[pathname];
      } else if (pathname.startsWith('/media/')) {
        base = resolve(canonicalRoot, 'data/media'); requested = pathname.slice(7);
      } else if (pathname.startsWith('/clips/')) {
        base = resolve(canonicalRoot, 'data/clips'); requested = pathname.slice(7);
      } else {
        return json(res, 404, { error: '找不到內容' });
      }
      const file = await isolatedFile(base, requested);
      const contentType = MIME_TYPES[extname(requested).toLowerCase()];
      if (!file || !contentType) return json(res, 404, { error: '找不到內容' });
      const headers = { 'Content-Type': contentType, 'Accept-Ranges': 'bytes' };
      let range = null;
      if (req.headers.range) {
        range = parseRange(req.headers.range, file.size);
        if (!range) { res.writeHead(416, { 'Content-Range': `bytes */${file.size}` }); return res.end(); }
        headers['Content-Range'] = `bytes ${range.start}-${range.end}/${file.size}`;
        headers['Content-Length'] = range.end - range.start + 1;
      } else headers['Content-Length'] = file.size;
      res.writeHead(range ? 206 : 200, headers);
      if (req.method === 'HEAD') return res.end();
      const stream = createReadStream(file.path, range || {});
      stream.on('error', () => res.destroy());
      res.on('close', () => stream.destroy());
      stream.pipe(res);
    } catch (error) {
      if (res.headersSent) return res.destroy();
      if (error instanceof PhotoFeedbackError) return json(res, error.statusCode, { error: error.message });
      if (error instanceof ManualPhotoError) {
        return json(res, error.statusCode, { error: error.message, ...(error.statusCode === 409 ? { status: manualPhoto.getStatus() } : {}) });
      }
      if (error instanceof ManualFetchError) {
        return json(res, error.statusCode, { error: error.message, ...(error.statusCode === 409 ? { status: manualFetch.getStatus() } : {}) });
      }
      if (['/api/manual-fetch', '/api/manual-fetch/post', '/api/manual-fetch/options', '/api/manual-fetch/status'].includes(pathname)) {
        return json(res, 500, { error: '手動照片工作暫時無法使用，請稍後重試。' });
      }
      if (['/api/manual-photo', '/api/manual-photo/status'].includes(pathname)) return json(res, 500, { error: '手動加入照片暫時無法使用，請稍後重試。' });
      if (pathname === '/api/photo-feedback') return json(res, 503, { error: '照片標記尚未確認保存，請重新讀取後重試。' });
      const code = ['ENOENT', 'ENOTDIR', 'EACCES'].includes(error.code) ? 404 : error instanceof URIError ? 400 : 500;
      json(res, code, { error: code === 500 ? '相簿資料讀取失敗，請檢查 data/manifest.json' : '找不到內容' });
    }
  };
}

export function createGalleryServer(options = {}) {
  return http.createServer(createGalleryHandler(options));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT || 4173);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT 必須介於 1 至 65535');
  const server = createGalleryServer();
  server.on('error', error => { console.error(`SunnyEgg 無法啟動：${error.message}`); process.exitCode = 1; });
  server.listen(port, '127.0.0.1', () => console.log(`SunnyEgg 已啟動：http://127.0.0.1:${port}`));
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close());
}
