import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { initializeTestEnvironment, assertFails, assertSucceeds } from '@firebase/rules-unit-testing';
import { getDoc, getDocs, setDoc, updateDoc, doc, collection, query, where } from 'firebase/firestore';
import { FirestoreRepository } from '../cloud/repository.mjs';
import { browserDatabase } from '../cloud/browser-firestore.js';

const emulator = !!process.env.FIRESTORE_EMULATOR_HOST;
test('deployed Firestore rules protect photo selection and administrator operations', { skip: !emulator }, async t => {
  const env = await initializeTestEnvironment({ projectId: 'demo-sunnyegg', firestore: { rules: await readFile(new URL('../firestore.rules', import.meta.url), 'utf8') } });
  t.after(() => env.cleanup());
  const google = { email: 'eggttball@gmail.com', email_verified: true, firebase: { sign_in_provider: 'google.com' } };
  const admin = env.authenticatedContext('admin', google).firestore();
  const anonymous = env.unauthenticatedContext().firestore();
  const base = { key: 'fb_1', postId: 'post', assetId: 'a', members: ['post:a'], userExcluded: false, revision: 0, status: 'published', selection: 'candidate', objectPath: 'photos/fb_1/image.jpg', pendingObjectPath: null, publicationToken: null, objectsToDelete: [] };
  await env.withSecurityRulesDisabled(async ctx => {
    await setDoc(doc(ctx.firestore(), 'photos/fb_1'), base);
    await setDoc(doc(ctx.firestore(), 'photos/fb_2'), { ...base, key: 'fb_2', status: 'excluded', userExcluded: true });
    await setDoc(doc(ctx.firestore(), 'runtime/worker'), { owner: 'worker', leaseUntil: Date.now() + 120000 });
  });
  await assertSucceeds(getDocs(query(collection(anonymous, 'photos'), where('status', '==', 'published'), where('userExcluded', '==', false))));
  await assertFails(getDoc(doc(anonymous, 'photos/fb_2')));
  await assertFails(getDocs(collection(anonymous, 'photos')));
  await assertFails(getDoc(doc(anonymous, 'runtime/worker')));
  await assertFails(updateDoc(doc(anonymous, 'photos/fb_1'), { userExcluded: true }));
  for (const claims of [{ ...google, email: 'other@gmail.com' }, { ...google, email_verified: false }, { ...google, firebase: { sign_in_provider: 'password' } }]) {
    const other = env.authenticatedContext('other', claims).firestore();
    await assertFails(getDoc(doc(other, 'runtime/worker')));
    await assertFails(setDoc(doc(other, 'jobs/x'), { state: 'queued' }));
  }
  await assertSucceeds(getDocs(collection(admin, 'photos')));
  await assertFails(updateDoc(doc(admin, 'photos/fb_1'), { objectPath: 'photos/fb_2/image.jpg' }));
  await assertFails(setDoc(doc(admin, 'photos/new'), base));
  const repository = new FirestoreRepository(browserDatabase(admin));
  await assertSucceeds(repository.setExclusion('fb_1', true));
  await assertFails(getDoc(doc(anonymous, 'photos/fb_1')));
  await assertSucceeds(repository.setExclusion('fb_1', false));
  await assertSucceeds(repository.enqueueJob('photo', { photoId: '123' }, { id: 'manual' }));
  await assertFails(updateDoc(doc(admin, 'jobs/manual'), { state: 'complete' }));
  await assertFails(setDoc(doc(admin, 'runtime/worker'), { owner: 'admin', leaseUntil: Date.now() + 120000 }));
  await env.withSecurityRulesDisabled(async ctx => {
    await updateDoc(doc(ctx.firestore(), 'jobs/manual'), { state: 'complete' });
    await setDoc(doc(ctx.firestore(), 'jobs/daily'), { id: 'daily', kind: 'fetch', input: { startDate: '2026-11-04', endDate: '2026-11-06', timeZone: 'Asia/Taipei' }, state: 'awaiting_approval' });
    await updateDoc(doc(ctx.firestore(), 'runtime/control'), { pendingApprovalId: 'daily' });
  });
  await assertSucceeds(repository.approveDailyJob('daily'));
  await assertFails(updateDoc(doc(admin, 'jobs/daily'), { state: 'running', owner: 'admin' }));
});
