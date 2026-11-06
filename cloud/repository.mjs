const randomUUID = () => globalThis.crypto.randomUUID();
import { validId } from './gallery.mjs';

export class FirestoreRepository {
  constructor(db, { clock = () => Date.now(), deferPublication = false, fence = null } = {}) {
    this.db = db; this.clock = clock; this.deferPublication = deferPublication; this.fence = fence;
  }
  forWorker(owner, jobId = null) {
    return new FirestoreRepository(this.db, { clock: this.clock, deferPublication: this.deferPublication, fence: { owner, jobId } });
  }
  async checkFence(tx) {
    if (!this.fence) return;
    const worker = (await tx.get(this.db.doc('runtime/worker'))).data();
    const job = this.fence.jobId ? (await tx.get(this.db.doc(`jobs/${this.fence.jobId}`))).data() : null;
    if (!worker || worker.owner !== this.fence.owner || worker.leaseUntil <= this.clock()
        || (this.fence.jobId && (!job || job.owner !== this.fence.owner || job.state !== 'running' || job.leaseUntil <= this.clock()))) {
      throw new Error('Worker lease lost');
    }
  }
  async assertOwnership() { await this.db.runTransaction(tx => this.checkFence(tx)); }
  photo(key) { if (!validId(key)) throw new Error('Invalid photo key'); return this.db.doc(`photos/${key}`); }
  async getPhoto(key) { const snap = await this.photo(key).get(); return snap.exists ? { ...snap.data(), key: snap.id } : null; }
  async listPhotos() { return (await this.db.collection('photos').get()).docs.map(snap => ({ ...snap.data(), key: snap.id })); }
  async listDeletions() { return (await this.db.collection('photos').where('objectsToDelete', '!=', []).get()).docs.map(snap => ({ ...snap.data(), key: snap.id })); }
  async findPhoto(postId, assetId) {
    const rows = await this.db.collection('photos').where('members', 'array-contains', `${postId}:${assetId}`).limit(1).get();
    return rows.docs.length ? { ...rows.docs[0].data(), key: rows.docs[0].id } : null;
  }
  async registerPhoto(record) {
    const { path, key, userExcluded, ...metadata } = record;
    const ref = this.photo(key);
    return this.db.runTransaction(async tx => {
      await this.checkFence(tx);
      const snap = await tx.get(ref), previous = snap.exists ? snap.data() : null;
      const value = { ...(previous || {}), ...metadata, key,
        members: [...new Set([...(previous?.members || []), ...record.members])],
        userExcluded: previous ? previous.userExcluded === true : userExcluded === true,
        revision: previous?.revision || 0,
        status: previous?.status || (userExcluded ? 'excluded' : 'pending'),
        objectsToDelete: previous?.objectsToDelete || [], updatedAt: this.clock(),
      };
      tx.set(ref, value); return value;
    });
  }
  async setExclusion(key, excluded, { expectedRevision } = {}) {
    const ref = this.photo(key);
    return this.db.runTransaction(async tx => {
      await this.checkFence(tx);
      const snap = await tx.get(ref);
      const signalRef = this.db.doc('runtime/feedback');
      const signal = (await tx.get(signalRef)).data() || {};
      if (!snap.exists) throw Object.assign(new Error('Photo not found'), { statusCode: 404 });
      const current = snap.data();
      if (expectedRevision !== undefined && (current.revision || 0) !== expectedRevision) return { ...current, key };
      if ((current.userExcluded === true) === excluded) return { ...current, key };
      const objectsToDelete = excluded ? [...new Set([...(current.objectsToDelete || []), current.objectPath, current.pendingObjectPath].filter(Boolean))] : current.objectsToDelete || [];
      const next = { ...current, key, userExcluded: excluded, revision: (current.revision || 0) + 1,
        status: excluded ? 'excluded' : 'pending', objectsToDelete, objectPath: null,
        pendingObjectPath: null, publicationToken: null, updatedAt: this.clock() };
      tx.set(ref, next);
      tx.set(signalRef, { revision: (signal.revision || 0) + 1 });
      return next;
    });
  }
  async beginPublication(key, imageHash, objectPath) {
    const ref = this.photo(key);
    return this.db.runTransaction(async tx => {
      await this.checkFence(tx);
      const snap = await tx.get(ref), current = snap.data();
      if (!snap.exists || current.userExcluded) return null;
      if (current.status === 'published' && current.imageHash === imageHash && current.objectPath) return { cached: true };
      if (current.status === 'staged' && current.pendingPayload?.imageHash === imageHash && current.pendingObjectPath) return { cached: true };
      const token = randomUUID();
      const next = { ...current, publicationToken: token, pendingObjectPath: objectPath,
        objectsToDelete: [...new Set([...(current.objectsToDelete || []), current.pendingObjectPath].filter(path => path && path !== objectPath))], updatedAt: this.clock() };
      tx.set(ref, next); return { token, revision: current.revision || 0 };
    });
  }
  async feedbackVersion() { return (await this.db.doc('runtime/feedback').get()).data()?.revision || 0; }
  async finishPublication(key, reservation, { objectPath, imageHash, contentType, size }) {
    const ref = this.photo(key);
    return this.db.runTransaction(async tx => {
      await this.checkFence(tx);
      const snap = await tx.get(ref), current = snap.data();
      if (!snap.exists || current.userExcluded || current.revision !== reservation.revision || current.publicationToken !== reservation.token) return false;
      if (this.deferPublication) {
        tx.set(ref, { ...current, status: 'staged', pendingPayload: { objectPath, imageHash, contentType, size }, updatedAt: this.clock() });
        return true;
      }
      const obsolete = [current.objectPath, ...(current.objectsToDelete || [])].filter(path => path && path !== objectPath);
      tx.set(ref, { ...current, status: 'published', objectPath, imageHash, contentType, size,
        publicationToken: null, pendingObjectPath: null, objectsToDelete: [...new Set(obsolete)], updatedAt: this.clock() });
      return true;
    });
  }
  async unpublish(key) {
    const ref = this.photo(key);
    await this.db.runTransaction(async tx => {
      await this.checkFence(tx);
      const snap = await tx.get(ref); if (!snap.exists) return;
      const current = snap.data();
      if (current.userExcluded || current.status === 'unselected') return;
      tx.set(ref, { ...current, status: 'unselected', revision: (current.revision || 0) + 1,
        objectsToDelete: [...new Set([...(current.objectsToDelete || []), current.objectPath, current.pendingObjectPath].filter(Boolean))],
        objectPath: null, pendingObjectPath: null, publicationToken: null });
    });
  }
  async acknowledgeDeletion(key, objectPath) {
    const ref = this.photo(key);
    await this.db.runTransaction(async tx => {
      await this.checkFence(tx);
      const snap = await tx.get(ref); if (!snap.exists) return;
      const current = snap.data();
      tx.set(ref, { objectsToDelete: (current.objectsToDelete || []).filter(path => path !== objectPath) }, { merge: true });
    });
  }
  async completeDeployment(key, reservation, payload) {
    const published = new FirestoreRepository(this.db, { clock: this.clock });
    return published.finishPublication(key, reservation, payload);
  }
  async pendingApproval() {
    const control = (await this.db.doc('runtime/control').get()).data() || {};
    if (!control.pendingApprovalId) return null;
    return (await this.db.doc(`jobs/${control.pendingApprovalId}`).get()).data() || null;
  }
  async approveDailyJob(id) {
    const ref = this.db.doc(`jobs/${id}`), controlRef = this.db.doc('runtime/control');
    return this.db.runTransaction(async tx => {
      const job = (await tx.get(ref)).data(), control = (await tx.get(controlRef)).data() || {};
      const active = control.activeJobId ? (await tx.get(this.db.doc(`jobs/${control.activeJobId}`))).data() : null;
      if (!job || job.state !== 'awaiting_approval' || control.pendingApprovalId !== id) throw new Error('No pending approval');
      if (active && ['queued', 'running'].includes(active.state)) throw Object.assign(new Error('已有照片工作排隊或進行中。'), { statusCode: 409 });
      const next = { ...job, state: 'queued', approvedAt: this.clock(), message: '已同意執行，等待本機 worker。' };
      tx.set(ref, next); tx.set(controlRef, { ...control, activeJobId: id, latestFetch: id, pendingApprovalId: null });
      return next;
    });
  }
  async enqueueJob(kind, input, { id = randomUUID(), awaitingApproval = false } = {}) {
    if (!['fetch', 'post', 'photo'].includes(kind) || !validId(id)) throw new Error('Invalid job');
    const controlRef = this.db.doc('runtime/control'), jobRef = this.db.doc(`jobs/${id}`);
    return this.db.runTransaction(async tx => {
      const control = (await tx.get(controlRef)).data() || {};
      const existing = await tx.get(jobRef);
      const active = control.activeJobId ? await tx.get(this.db.doc(`jobs/${control.activeJobId}`)) : null;
      if (existing.exists) return existing.data();
      if (!awaitingApproval && active?.exists && ['queued', 'running'].includes(active.data().state)) throw Object.assign(new Error('已有照片工作排隊或進行中。'), { statusCode: 409 });
      const job = { id, kind, input, state: awaitingApproval ? 'awaiting_approval' : 'queued', createdAt: this.clock(), message: awaitingApproval ? '每日照片工作等待管理員同意。' : '工作已排入佇列，等待本機 worker。', progress: { phase: 'preparing' } };
      tx.set(jobRef, job);
      tx.set(controlRef, awaitingApproval ? { ...control, pendingApprovalId: id } : { ...control, activeJobId: id, [kind === 'photo' ? 'latestPhoto' : 'latestFetch']: id });
      return job;
    });
  }
  async claimJob(owner) {
    const controlRef = this.db.doc('runtime/control');
    return this.db.runTransaction(async tx => {
      const worker = (await tx.get(this.db.doc('runtime/worker'))).data();
      if (!worker || worker.owner !== owner || worker.leaseUntil <= this.clock()) return null;
      const control = (await tx.get(controlRef)).data();
      if (!control?.activeJobId) return null;
      const ref = this.db.doc(`jobs/${control.activeJobId}`), snap = await tx.get(ref);
      if (!snap.exists) return null;
      const job = snap.data();
      if (job.state !== 'queued' && !(job.state === 'running' && (job.leaseUntil <= this.clock() || job.owner !== worker.owner))) return null;
      const next = { ...job, state: 'running', owner, leaseUntil: this.clock() + 120000, startedAt: job.startedAt || this.clock(), message: '本機 worker 正在處理照片。' };
      tx.set(ref, next); return next;
    });
  }
  async updateJob(id, owner, patch) {
    const ref = this.db.doc(`jobs/${id}`);
    return this.db.runTransaction(async tx => {
      const worker = (await tx.get(this.db.doc('runtime/worker'))).data();
      if (!worker || worker.owner !== owner || worker.leaseUntil <= this.clock()) return false;
      const snap = await tx.get(ref), current = snap.data();
      if (!snap.exists || current.state !== 'running' || current.owner !== owner || current.leaseUntil <= this.clock()) return false;
      tx.set(ref, { ...current, ...patch, leaseUntil: this.clock() + 120000,
        ...(['complete', 'partial', 'blocked', 'failed'].includes(patch.state) ? { finishedAt: this.clock() } : {}) });
      return true;
    });
  }
  async heartbeat(owner) {
    const ref = this.db.doc('runtime/worker');
    return this.db.runTransaction(async tx => {
      const snap = await tx.get(ref), current = snap.data();
      if (snap.exists && current.owner !== owner && current.leaseUntil > this.clock()) return false;
      tx.set(ref, { owner, lastSeenAt: this.clock(), leaseUntil: this.clock() + 120000 }); return true;
    });
  }
  async workerOnline() {
    const value = (await this.db.doc('runtime/worker').get()).data();
    return !!value && value.leaseUntil > this.clock();
  }
  async releaseWorker(owner) {
    const ref = this.db.doc('runtime/worker');
    await this.db.runTransaction(async tx => {
      const snap = await tx.get(ref);
      if (snap.exists && snap.data().owner === owner) tx.set(ref, { leaseUntil: 0 }, { merge: true });
    });
  }
  async jobStatus(group) {
    const control = (await this.db.doc('runtime/control').get()).data() || {};
    const id = control[group === 'photo' ? 'latestPhoto' : 'latestFetch'];
    const job = id ? (await this.db.doc(`jobs/${id}`).get()).data() : null;
    const workerOnline = await this.workerOnline();
    if (!job) return { state: 'idle', workerOnline, message: workerOnline ? '尚未啟動照片工作。' : '本機 worker 尚未連線；工作送出後會等待它上線。' };
    return { state: job.state === 'queued' ? 'running' : job.state, queued: job.state === 'queued', workerOnline,
      runId: job.id, startedAt: new Date(job.startedAt || job.createdAt).toISOString(),
      ...(job.finishedAt ? { finishedAt: new Date(job.finishedAt).toISOString() } : {}),
      ...(job.kind === 'fetch' ? { range: job.input } : job.kind === 'post' ? { targetType: 'post' } : {}),
      message: job.message, ...(job.counts ? { counts: job.counts } : {}), ...(job.progress ? { progress: job.progress } : {}),
      ...(job.action ? { action: job.action } : {}), ...(job.reaction ? { reaction: job.reaction } : {}) };
  }
}
