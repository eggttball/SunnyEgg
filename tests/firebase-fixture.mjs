import { Readable } from 'node:stream';

export class MemoryFirestore {
  constructor() { this.values = new Map(); this.tail = Promise.resolve(); }
  doc(path) {
    return { path, id: path.split('/').at(-1),
      get: async () => this.snapshot(path),
      set: async (value, options) => this.set(path, value, options),
    };
  }
  snapshot(path) { const value = this.values.get(path); return { exists: value !== undefined, id: path.split('/').at(-1), data: () => structuredClone(value) }; }
  set(path, value, options) { this.values.set(path, structuredClone(options?.merge ? { ...this.values.get(path), ...value } : value)); }
  collection(name) {
    const query = predicates => ({
      where: (field, operation, value) => query([...predicates, row => operation === 'array-contains' ? row[field]?.includes(value) : operation === '!=' ? JSON.stringify(row[field]) !== JSON.stringify(value) : row[field] === value]),
      limit: count => ({ get: async () => ({ docs: (await query(predicates).get()).docs.slice(0, count) }) }),
      get: async () => ({ docs: [...this.values].filter(([path, row]) => path.startsWith(name + '/') && !path.slice(name.length + 1).includes('/') && predicates.every(fn => fn(row))).map(([path]) => this.snapshot(path)) }),
    });
    return { ...query([]), doc: id => this.doc(name + '/' + id) };
  }
  async runTransaction(fn) {
    const previous = this.tail;
    let unlock; this.tail = new Promise(resolve => { unlock = resolve; });
    await previous;
    const writes = [];
    try {
      const value = await fn({ get: ref => ref.get(), set: (ref, body, options) => { writes.push(() => this.set(ref.path, body, options)); } });
      writes.forEach(write => write()); return value;
    } finally { unlock(); }
  }
}

export class MemoryBucket {
  constructor() { this.objects = new Map(); this.failDelete = false; this.onSave = null; }
  file(path) {
    return {
      save: async (bytes, options) => { this.objects.set(path, { bytes: Buffer.from(bytes), metadata: options.metadata }); await this.onSave?.(path); },
      delete: async () => { if (this.failDelete) throw new Error('Storage unavailable'); this.objects.delete(path); },
      download: async () => [this.objects.get(path)?.bytes],
      createReadStream: () => Readable.from([this.objects.get(path)?.bytes || Buffer.alloc(0)]),
      getMetadata: async () => { const object = this.objects.get(path); if (!object) throw Object.assign(new Error('missing'), { code: 404 }); return [{ size: object.bytes.length, ...object.metadata }]; },
    };
  }
}
