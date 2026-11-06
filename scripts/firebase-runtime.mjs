import { resolve } from 'node:path';
import { access } from 'node:fs/promises';
import { FirestoreRepository } from '../cloud/repository.mjs';

export async function firebaseRuntime(root, { media = true } = {}) {
  try { process.loadEnvFile(resolve(root, '.env')); } catch (error) { if (error.code !== 'ENOENT') throw new Error('無法讀取本機 Firebase 設定。'); }
  const { initializeApp, applicationDefault, getApps } = await import('firebase-admin/app');
  const { getFirestore } = await import('firebase-admin/firestore');
  if (process.env.GOOGLE_APPLICATION_CREDENTIALS) {
    try { await access(process.env.GOOGLE_APPLICATION_CREDENTIALS); } catch { throw new Error('GOOGLE_APPLICATION_CREDENTIALS 指向的檔案無法讀取。'); }
  }
  const app = getApps()[0] || initializeApp({ credential: applicationDefault(), projectId: 'sunnyegg' });
  const repository = new FirestoreRepository(getFirestore(app), { deferPublication: true });
  if (!media) return { repository };
  const { GitMediaBucket } = await import('./firebase-media.mjs');
  if (!process.env.SUNNYEGG_MEDIA_DIR) throw new Error('請設定 SUNNYEGG_MEDIA_DIR 為私人照片 repo 的本機絕對路徑。');
  const bucket = new GitMediaBucket(process.env.SUNNYEGG_MEDIA_DIR);
  await bucket.verify();
  return { repository, bucket };
}
