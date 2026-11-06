import { initializeApp } from 'firebase/app';
import { getAuth, GoogleAuthProvider, signInWithPopup, signOut, onAuthStateChanged } from 'firebase/auth';
import { getFirestore, collection, doc, query, where, getDocs, onSnapshot } from 'firebase/firestore';
import { browserDatabase } from '../cloud/browser-firestore.js';
import { FirestoreRepository } from '../cloud/repository.mjs';
import { publicGallery, isAdministrator } from '../cloud/gallery.mjs';
import { createOptions, validateRange, normalizePostUrl, photoIdFromUrl, validateFeedback } from '../cloud/validation.mjs';

const app = initializeApp({
  apiKey: 'AIzaSyCqKJAQMRKJhNhOT2uI1VgsEK76Q1U6kyI',
  authDomain: 'sunnyegg.firebaseapp.com', projectId: 'sunnyegg', messagingSenderId: '252995201906',
  appId: '1:252995201906:web:d638a3b4f1d6ef04ce0beb',
});
const auth = getAuth(app), db = getFirestore(app), repository = new FirestoreRepository(browserDatabase(db));
const provider = new GoogleAuthProvider();
provider.setCustomParameters({ prompt: 'select_account' });
const listeners = new Set(), galleryListeners = new Set();
let allowed = false, sequence = 0, stopGallery = () => {}, stopControl = () => {}, stopApproval = () => {};
const controlsRequested = new URLSearchParams(window.location.search).get('ctrl') === '1';
const panel = document.querySelector('#admin-auth'), message = document.querySelector('#admin-auth-message');
const login = document.querySelector('#admin-sign-in'), logout = document.querySelector('#admin-sign-out');
panel.hidden = !controlsRequested;
const approval = document.createElement('section');
approval.className = 'daily-approval'; approval.hidden = true;
approval.setAttribute('aria-live', 'polite');
const approvalText = document.createElement('p'), approve = document.createElement('button');
approve.className = 'button'; approve.type = 'button'; approve.textContent = '同意執行';
approval.append(approvalText, approve); panel.after(approval);
let pending = null;
const notify = value => { allowed = value; listeners.forEach(listener => listener(value)); };
const photosQuery = admin => admin ? collection(db, 'photos') : query(collection(db, 'photos'), where('status', '==', 'published'), where('userExcluded', '==', false));
function watchGallery() {
  stopGallery(); let first = true;
  stopGallery = onSnapshot(photosQuery(allowed && controlsRequested), () => {
    if (first) { first = false; return; }
    galleryListeners.forEach(fn => fn());
  }, () => {});
}
function watchApproval() {
  stopControl(); stopApproval(); approval.hidden = true;
  if (!allowed) return;
  stopControl = onSnapshot(doc(db, 'runtime/control'), control => {
    stopApproval(); pending = null; approval.hidden = true;
    const id = control.data()?.pendingApprovalId;
    if (!id) return;
    stopApproval = onSnapshot(doc(db, 'jobs', id), value => {
      pending = value.data(); approval.hidden = !allowed || pending?.state !== 'awaiting_approval';
      if (!approval.hidden) approvalText.textContent = `${pending.input.startDate} 至 ${pending.input.endDate}：每日照片工作已就緒，等待你同意。`;
    }, () => { approval.hidden = true; });
  }, () => { approval.hidden = true; });
}
approve.addEventListener('click', async () => {
  if (!allowed || !pending || pending.state !== 'awaiting_approval') return;
  approve.disabled = true;
  try { await repository.approveDailyJob(pending.id); galleryListeners.forEach(fn => fn()); window.dispatchEvent(new Event('sunnyegg-job-queued')); }
  catch { approvalText.textContent = '無法同意執行；請確認沒有其他工作進行中，再重試。'; }
  finally { approve.disabled = false; }
});
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
window.SunnyEggCloud = {
  subscribe(listener) { listeners.add(listener); listener(allowed); return () => listeners.delete(listener); },
  onGalleryChange(listener) { galleryListeners.add(listener); return () => galleryListeners.delete(listener); },
  async request(url, init = {}) {
    const path = new URL(url, window.location.origin).pathname, method = init.method || 'GET';
    try {
      if (path === '/api/gallery' && method === 'GET') {
        const rows = (await getDocs(photosQuery(allowed && controlsRequested))).docs.map(row => ({ ...row.data(), key: row.id }));
        return response(publicGallery(rows, { admin: allowed && controlsRequested }));
      }
      if (!allowed || !auth.currentUser || !controlsRequested) return response({ error: '請先登入管理員帳號。' }, 403);
      if (method === 'GET') {
        if (path === '/api/manual-fetch/options') return response(createOptions());
        if (path === '/api/manual-fetch/status') return response(await repository.jobStatus('fetch'));
        if (path === '/api/manual-photo/status') return response(await repository.jobStatus('photo'));
      }
      if (method !== 'POST') return response({ error: '不支援的操作。' }, 404);
      const input = JSON.parse(init.body || '{}');
      if (path === '/api/photo-feedback') {
        validateFeedback(input);
        const photo = await repository.findPhoto(input.postId, input.assetId);
        if (!photo) return response({ error: '找不到照片。' }, 404);
        await repository.setExclusion(photo.key, input.excluded);
        return response(input);
      }
      const kind = path === '/api/manual-fetch' ? 'fetch' : path === '/api/manual-fetch/post' ? 'post' : path === '/api/manual-photo' ? 'photo' : null;
      if (!kind) return response({ error: '不支援的操作。' }, 404);
      const jobInput = kind === 'fetch' ? validateRange(input) : kind === 'post' ? { postUrl: normalizePostUrl(input.url) } : { photoId: photoIdFromUrl(input.url) };
      try { await repository.enqueueJob(kind, jobInput); }
      catch (error) { if (error.statusCode === 409) return response({ status: await repository.jobStatus(kind === 'photo' ? 'photo' : 'fetch') }, 409); throw error; }
      return response(await repository.jobStatus(kind === 'photo' ? 'photo' : 'fetch'), 202);
    } catch (error) {
      if (error.code === 'permission-denied') { message.textContent = '管理員權限不足，請確認登入帳號與 Firestore rules。'; notify(false); return response({ error: '沒有操作權限。' }, 403); }
      return response({ error: error.statusCode === 400 ? error.message : '雲端操作未完成，請稍後再試。' }, error.statusCode || 503);
    }
  },
};
login.addEventListener('click', async () => {
  login.disabled = true;
  try { await signInWithPopup(auth, provider); }
  catch (error) { message.textContent = error.code === 'auth/popup-blocked' ? '請允許此網站開啟登入視窗。' : 'Google 登入尚未完成，請重試。'; }
  finally { login.disabled = false; }
});
logout.addEventListener('click', () => { void signOut(auth); });
watchGallery();
if (controlsRequested) onAuthStateChanged(auth, async user => {
  const current = ++sequence; notify(false); watchApproval();
  logout.hidden = !user; message.textContent = user ? '正在確認管理員權限…' : '請使用管理員 Google 帳號登入。';
  if (!user) { watchGallery(); return; }
  try {
    const result = await user.getIdTokenResult();
    if (current !== sequence) return;
    if (!isAdministrator(result.claims, ['eggttball@gmail.com'])) throw new Error('Not administrator');
    // A real protected read confirms the deployed rules, beyond the UI check.
    await repository.workerOnline();
    if (current !== sequence) return;
    message.textContent = `已登入：${user.email}`; notify(true); watchGallery(); watchApproval();
  } catch { if (current === sequence) { message.textContent = '此帳號沒有管理員權限，或 Firestore 尚未設定完成。'; watchGallery(); } }
});
