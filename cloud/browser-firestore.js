import { doc, collection, query, where, limit, getDoc, getDocs, setDoc, runTransaction } from 'firebase/firestore';
// Adapt the browser SDK to the same transaction logic used by the local worker.
const snapshot = value => ({ id: value.id, exists: value.exists(), data: () => value.data() });
export function browserDatabase(db) {
  const reference = path => ({ path, id: path.split('/').at(-1), get: async () => snapshot(await getDoc(doc(db, path))), set: (value, options) => setDoc(doc(db, path), value, options || {}) });
  const collectionQuery = (name, filters = []) => ({
    where: (field, operation, value) => collectionQuery(name, [...filters, where(field, operation, value)]),
    limit: count => collectionQuery(name, [...filters, limit(count)]),
    get: async () => ({ docs: (await getDocs(query(collection(db, name), ...filters))).docs.map(snapshot) }),
  });
  return {
    doc: reference, collection: collectionQuery,
    runTransaction: fn => runTransaction(db, tx => fn({
      get: async ref => snapshot(await tx.get(doc(db, ref.path))),
      set: (ref, body, options) => options ? tx.set(doc(db, ref.path), body, options) : tx.set(doc(db, ref.path), body),
    })),
  };
}
