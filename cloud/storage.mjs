export async function deleteQueuedObjects(repository, bucket, onlyKey = null) {
  let removed = 0;
  const photos = onlyKey ? [await repository.getPhoto(onlyKey)].filter(Boolean) : await repository.listDeletions();
  for (const photo of photos) {
    for (const path of photo.objectsToDelete || []) {
      if (typeof path !== 'string' || !path.startsWith(`photos/${photo.key}/`)) throw new Error('Invalid deletion path');
      await bucket.file(path).delete({ ignoreNotFound: true });
      if (!bucket.deferDeletion) await repository.acknowledgeDeletion(photo.key, path);
      removed++;
    }
  }
  return removed;
}
