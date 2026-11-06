import { createHash, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { lstat, open, readFile, realpath } from 'node:fs/promises';
import { extname, relative, resolve, sep } from 'node:path';
import { selectPublications } from '../cloud/gallery.mjs';
import { deleteQueuedObjects } from '../cloud/storage.mjs';

const TYPES = { '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp' };
const MAX_IMAGE = 50 * 1024 * 1024;

async function original(root, path) {
  if (typeof path !== 'string' || !/^media\//.test(path) || path.includes('\\') || path.split('/').some(part => !part || part === '.' || part === '..')) throw new Error('Invalid photo path');
  const base = await realpath(resolve(root, 'data/media'));
  const target = resolve(root, 'data', path), inside = relative(base, target);
  if (!inside || inside.startsWith(`..${sep}`) || inside === '..' || inside.startsWith(sep)) throw new Error('Unsafe photo path');
  const info = await lstat(target);
  if (!info.isFile() || info.isSymbolicLink() || info.size > MAX_IMAGE) throw new Error('Invalid photo original');
  const canonical = await realpath(target);
  if (canonical !== target) throw new Error('Symlinked photo original');
  const extension = extname(path).toLowerCase(), contentType = TYPES[extension];
  if (!contentType) throw new Error('Unsupported image type');
  const handle = await open(target, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  let bytes;
  try { bytes = await handle.readFile(); } finally { await handle.close(); }
  if (bytes.length > MAX_IMAGE) throw new Error('Image too large');
  const jpeg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  const png = bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]));
  const webp = bytes.subarray(0, 4).toString() === 'RIFF' && bytes.subarray(8, 12).toString() === 'WEBP';
  if (!(contentType === 'image/jpeg' ? jpeg : contentType === 'image/png' ? png : webp)) throw new Error('Invalid image signature');
  return { bytes, contentType, extension, imageHash: createHash('sha256').update(bytes).digest('hex') };
}

export const deleteExcludedObjects = deleteQueuedObjects;

export async function syncGallery({ root, repository, bucket }) {
  bucket.setRepository?.(repository);
  const manifestPath = resolve(root, 'data/manifest.json');
  const info = await lstat(manifestPath);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 32 * 1024 * 1024) throw new Error('Invalid local manifest');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  const selected = selectPublications(manifest);
  let feedbackState = {};
  try { feedbackState = JSON.parse(await readFile(resolve(root, 'data/firebase-feedback-state.json'), 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  // Validate every selected original before writing cloud metadata or pruning.
  const prepared = [];
  for (const record of selected) {
    const image = record.userExcluded ? null : await original(root, record.path);
    const existing = !record.sourcePhotoId ? await repository.findPhoto(record.postId, record.assetId) : null;
    const key = existing?.key || (record.sourcePhotoId || !image ? record.key : `sha_${image.imageHash}`);
    prepared.push({ record: { ...record, key }, image });
  }
  const keys = new Set(), counts = { uploaded: 0, cached: 0, excluded: 0, removed: 0 };
  for (const { record, image } of prepared) {
    keys.add(record.key);
    const registered = await repository.registerPhoto(record);
    if (record.userExcluded) await repository.setExclusion(record.key, true, { expectedRevision: feedbackState[record.key] || 0 });
    if (record.userExcluded || registered.userExcluded) { counts.excluded++; continue; }
    const objectPath = `photos/${record.key}/${image.imageHash}-${randomUUID()}${image.extension}`;
    const reservation = await repository.beginPublication(record.key, image.imageHash, objectPath);
    if (!reservation) continue;
    if (reservation.cached) { counts.cached++; continue; }
    try {
      await bucket.file(objectPath).save(image.bytes, { resumable: false, preconditionOpts: { ifGenerationMatch: 0 },
        metadata: { contentType: image.contentType, cacheControl: 'private, no-store' } });
      const published = await repository.finishPublication(record.key, reservation, { objectPath, imageHash: image.imageHash, contentType: image.contentType, size: image.bytes.length });
      if (!published) await bucket.file(objectPath).delete({ ignoreNotFound: true });
      else counts.uploaded++;
    } catch (error) {
      await bucket.file(objectPath).delete({ ignoreNotFound: true }).catch(() => {});
      throw error;
    }
  }
  for (const photo of await repository.listPhotos()) if (!keys.has(photo.key)) await repository.unpublish(photo.key);
  counts.removed = await deleteExcludedObjects(repository, bucket);
  await bucket.commit?.(repository);
  return counts;
}
