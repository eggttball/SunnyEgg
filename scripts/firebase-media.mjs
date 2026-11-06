import { createHash, randomUUID } from 'node:crypto';
import { hostname } from 'node:os';
import { attachAbort } from './process-abort.mjs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { lstat, mkdir, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, resolve } from 'node:path';
const execute = promisify(execFile);
export const MEDIA_REPOSITORY = 'eggttball/SunnyEggMedia';
export const validMediaPath = path => typeof path === 'string' && /^photos\/[A-Za-z0-9_-]{1,200}\/[A-Za-z0-9_-]{1,200}\.(jpg|jpeg|png|webp)$/.test(path);

// Clone only into a missing/empty destination; never repurpose a populated folder.
export async function ensureMediaCheckout(destination, clone) {
  if (typeof destination !== 'string' || !isAbsolute(destination)) throw new Error('Invalid media checkout directory');
  let exists = true;
  try {
    const info = await lstat(destination);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Unsafe media checkout directory');
  } catch (error) { if (error.code !== 'ENOENT') throw error; exists = false; }
  if (exists) {
    let git;
    try { git = await lstat(resolve(destination, '.git')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (git) {
      if (git.isSymbolicLink() || (!git.isDirectory() && !git.isFile())) throw new Error('Unsafe Git checkout');
      return;
    }
    if ((await readdir(destination)).length) throw new Error('Media destination is nonempty and is not a Git checkout');
  } else await mkdir(dirname(destination), { recursive: true });
  await clone();
  const git = await lstat(resolve(destination, '.git'));
  if (!git.isDirectory() || git.isSymbolicLink()) throw new Error('Media clone did not create a Git checkout');
}

async function safePath(root, path, create = false) {
  if (!validMediaPath(path)) throw new Error('Invalid media path');
  const base = await realpath(root);
  let parent = base;
  for (const part of path.split('/').slice(0, -1)) {
    parent = resolve(parent, part);
    if (create) await mkdir(parent, { recursive: true });
    const info = await lstat(parent);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Unsafe media directory');
  }
  const target = resolve(base, path);
  try { const info = await lstat(target); if (!info.isFile() || info.isSymbolicLink() || info.size > 50 * 1024 * 1024) throw new Error('Unsafe media file'); }
  catch (error) { if (error.code !== 'ENOENT' || !create) throw error; }
  return target;
}
async function jsonFile(path) {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 32 * 1024 * 1024) throw new Error('Invalid media manifest');
  const manifest = JSON.parse(await readFile(path, 'utf8'));
  if (manifest.version !== 1 || !Array.isArray(manifest.photos)) throw new Error('Invalid media manifest');
  return manifest;
}
const payload = photo => ({ objectPath: photo.objectPath, imageHash: photo.imageHash, contentType: photo.contentType, size: photo.size });
export function deploymentEligible(record, entry) {
  if (!record || record.userExcluded || !['candidate', 'manual'].includes(record.selection) || !validMediaPath(entry.objectPath)) return false;
  if (record.status === 'published') return record.objectPath === entry.objectPath && record.imageHash === entry.imageHash;
  return record.status === 'staged' && record.revision === entry.reservation?.revision && record.publicationToken === entry.reservation?.token
    && record.pendingObjectPath === entry.objectPath && record.pendingPayload?.imageHash === entry.imageHash;
}

// This directory contains only selected public assets. Never copy data/ wholesale.
export async function prepareDeployment({ repository, mediaRoot, dist }) {
  let manifest;
  try { manifest = await jsonFile(resolve(mediaRoot, 'manifest.json')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; manifest = { version: 1, photos: [] }; }
  const records = await repository.listPhotos(), byKey = new Map(records.map(row => [row.key, row]));
  const selected = manifest.photos.filter(entry => deploymentEligible(byKey.get(entry.key), entry));
  const paths = new Set(), keys = new Set(), prepared = [];
  for (const entry of selected) {
    if (paths.has(entry.objectPath) || keys.has(entry.key)) throw new Error('Duplicate media manifest identity');
    paths.add(entry.objectPath); keys.add(entry.key);
    const bytes = await readFile(await safePath(mediaRoot, entry.objectPath));
    if (bytes.length !== entry.size || createHash('sha256').update(bytes).digest('hex') !== entry.imageHash) throw new Error('Media hash mismatch');
    prepared.push({ entry, bytes });
  }
  if (records.some(row => row.status === 'published' && !row.userExcluded && !keys.has(row.key))) throw new Error('Published photo missing from media checkout; deployment cancelled');
  await rm(resolve(dist, 'media'), { recursive: true, force: true });
  for (const { entry, bytes } of prepared) {
    const path = resolve(dist, 'media', entry.objectPath);
    await mkdir(dirname(path), { recursive: true }); await writeFile(path, bytes);
  }
  return { version: 1, photos: selected, deleted: records.flatMap(row => (row.objectsToDelete || []).filter(path => !paths.has(path)).map(objectPath => ({ key: row.key, objectPath }))) };
}
export async function finalizeDeployment(repository, snapshot) {
  for (const entry of snapshot.photos) if (entry.reservation?.token) await repository.completeDeployment(entry.key, entry.reservation, payload(entry));
  for (const entry of snapshot.deleted) await repository.acknowledgeDeletion(entry.key, entry.objectPath);
}

export class GitMediaBucket {
  constructor(root, { git = execute } = {}) { this.root = resolve(root); this.git = git; this.deferDeletion = true; }
  setRepository(repository) { this.repository = repository; }
  async check() { await this.repository?.assertOwnership(); this.signal?.throwIfAborted(); }
  async acquireLock() {
    const path = resolve(this.root, await this.command(['rev-parse', '--git-path', 'sunnyegg-worker.lock']));
    const token = randomUUID(), body = { token, pid: process.pid, host: hostname() };
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        await writeFile(path, JSON.stringify(body), { flag: 'wx', mode: 0o600 });
        return async () => { const current = JSON.parse(await readFile(path, 'utf8')); if (current.token === token) await rm(path); };
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
        const recovery = path + '.recovery';
        await mkdir(recovery);
        try {
          const info = await lstat(path);
          if (!info.isFile() || info.isSymbolicLink() || info.size > 4096) throw new Error('Unsafe media worker lock');
          const current = JSON.parse(await readFile(path, 'utf8'));
          if (current.host !== hostname() || !Number.isSafeInteger(current.pid) || current.pid <= 0) throw new Error('Media checkout is locked');
          try { process.kill(current.pid, 0); throw new Error('Media checkout already has a local worker'); }
          catch (failure) { if (failure.code !== 'ESRCH') throw failure; }
          await rm(path);
        } finally { await rm(recovery, { recursive: true }); }
      }
    }
    throw new Error('Cannot acquire media checkout lock');
  }
  async command(args) {
    await this.check();
    let detach = () => {};
    try {
      const pending = this.git('git', args, { cwd: this.root, maxBuffer: 4 * 1024 * 1024, detached: process.platform !== 'win32' });
      if (pending.child) detach = attachAbort(pending.child, this.signal);
      return (await pending).stdout.trim();
    }
    catch { throw new Error('SunnyEggMedia Git operation failed; check local Git access'); }
    finally { detach(); }
  }
  async verify() {
    const remote = await this.command(['remote', 'get-url', 'origin']);
    if (!['git@github.com:eggttball/SunnyEggMedia.git', 'https://github.com/eggttball/SunnyEggMedia.git'].includes(remote)) throw new Error('Unexpected media repository');
    if (await this.command(['branch', '--show-current']) !== 'main') throw new Error('Media checkout must use main');
    // Verify privacy using the local GitHub CLI credential. No token enters browser code.
    let privacy;
    try { privacy = (await execute('gh', ['api', 'repos/' + MEDIA_REPOSITORY, '--jq', '.private'], { maxBuffer: 1024 })).stdout.trim(); }
    catch { throw new Error('Cannot verify SunnyEggMedia privacy; authenticate gh and grant repo access'); }
    if (privacy !== 'true') throw new Error('SunnyEggMedia must be private before uploading photos');
    const tracked = (await this.command(['ls-files'])).split('\n').filter(Boolean);
    if (tracked.some(path => !validMediaPath(path) && !['manifest.json', 'README.md', '.gitignore', '.github/workflows/notify-sunnyegg.yml'].includes(path))) throw new Error('Unexpected files in media repository');
  }
  file(path) {
    if (!validMediaPath(path)) throw new Error('Invalid media path');
    return {
      save: async bytes => { await this.check(); await writeFile(await safePath(this.root, path, true), bytes, { flag: 'wx', mode: 0o600 }); },
      delete: async () => { await this.check(); try { await rm(await safePath(this.root, path)); } catch (error) { if (error.code !== 'ENOENT') throw error; } },
    };
  }
  async commit(repository) {
    this.setRepository(repository);
    await this.check();
    const photos = (await repository.listPhotos()).filter(row => !row.userExcluded && ['published', 'staged'].includes(row.status)).map(row => ({
      key: row.key, ...(row.status === 'staged' ? { ...row.pendingPayload, reservation: { token: row.publicationToken, revision: row.revision } } : payload(row)),
    }));
    const keep = new Set(photos.map(row => row.objectPath));
    const walk = async dir => {
      let entries; try { entries = await readdir(resolve(this.root, dir), { withFileTypes: true }); } catch (error) { if (error.code === 'ENOENT') return; throw error; }
      for (const item of entries) {
        const path = dir + '/' + item.name;
        if (item.isSymbolicLink()) throw new Error('Symlink in media directory');
        if (item.isDirectory()) await walk(path);
        else { if (!validMediaPath(path)) throw new Error('Unexpected media file'); if (!keep.has(path)) await this.file(path).delete(); }
      }
    };
    await walk('photos');
    await this.check();
    await writeFile(resolve(this.root, 'manifest.json'), JSON.stringify({ version: 1, photos }, null, 2) + '\n', { mode: 0o600 });
    await mkdir(resolve(this.root, 'photos'), { recursive: true });
    await this.command(['add', '--', 'photos', 'manifest.json']);
    const staged = await this.command(['diff', '--cached', '--name-only']);
    if (staged.split('\n').filter(Boolean).some(path => path !== 'manifest.json' && !validMediaPath(path))) throw new Error('Unexpected staged files; media push cancelled');
    await repository.assertOwnership();
    if (staged) await this.command(['commit', '-m', 'Sync selected SunnyEgg photos']);
    // Retry previously committed but unpushed media after a connection failure.
    await this.command(['push', 'origin', 'main']);
  }
}
