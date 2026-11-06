export const SOURCE_PAGE = 'https://www.facebook.com/profile.php?id=100063963161862';
export const validId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(value);

export function isAdministrator(user, emails) {
  return !!user && user.email_verified === true && user.firebase?.sign_in_provider === 'google.com'
    && typeof user.email === 'string' && emails.map(email => email.trim().toLowerCase()).includes(user.email.toLowerCase());
}

export function sourcePhotoId(post, asset) {
  if (asset.sourcePhotoId != null) return typeof asset.sourcePhotoId === 'string' && /^\d+$/.test(asset.sourcePhotoId) ? asset.sourcePhotoId : null;
  const index = post.assets.indexOf(asset);
  const legacy = Array.isArray(post.dailyPhotoIds) && post.dailyPhotoIds.length === post.assets.length ? post.dailyPhotoIds[index] : null;
  return typeof legacy === 'string' && /^\d+$/.test(legacy) ? legacy : null;
}

export function selectPublications(manifest) {
  if (manifest?.version !== 1 || !Array.isArray(manifest.posts)) throw new Error('Invalid gallery manifest');
  const records = [], byId = new Map(), byPhoto = new Map();
  for (const post of manifest.posts) {
    if (post?.kind !== 'photos') continue;
    if (!validId(post.id) || !Array.isArray(post.assets)) throw new Error('Invalid photo post');
    for (const asset of post.assets) {
      if (asset?.type !== 'image') continue;
      if (!validId(asset.id) || byId.has(asset.id)) throw new Error('Ambiguous photo identity');
      const item = { post, asset, sourcePhotoId: sourcePhotoId(post, asset), group: null };
      item.group = [item];
      records.push(item); byId.set(asset.id, item);
    }
  }
  const join = (a, b) => {
    if (a.group === b.group) return;
    const merged = [...a.group, ...b.group];
    merged.forEach(item => { item.group = merged; });
  };
  for (const item of records) {
    if (!item.sourcePhotoId) continue;
    if (byPhoto.has(item.sourcePhotoId)) join(byPhoto.get(item.sourcePhotoId), item);
    else byPhoto.set(item.sourcePhotoId, item);
  }
  const chains = new Map();
  for (const item of records) {
    let current = item;
    const members = [], seen = new Set();
    while (current && !seen.has(current)) {
      seen.add(current); members.push(current);
      if (current.asset.duplicateOf == null) {
        if (!chains.has(current)) chains.set(current, new Set());
        members.forEach(member => chains.get(current).add(member));
        break;
      }
      current = byId.get(current.asset.duplicateOf);
    }
  }
  for (const [owner, members] of chains) {
    if (new Set([...members].map(item => item.sourcePhotoId).filter(Boolean)).size <= 1) members.forEach(item => join(owner, item));
  }
  const seen = new Set(), result = [];
  for (const item of records) {
    if (seen.has(item.group)) continue;
    seen.add(item.group);
    const candidates = item.group.filter(member => member.post.recognition?.results?.some(r => r?.assetId === member.asset.id && r.state === 'candidate'));
    const manual = item.group.some(member => member.asset.manualSelection === true);
    if (!manual && candidates.length === 0) continue;
    const canonical = item.group.find(member => member.asset.duplicateOf == null) || item;
    const photoId = item.group.map(member => member.sourcePhotoId).find(Boolean) || null;
    result.push({
      key: photoId ? `fb_${photoId}` : `asset_${canonical.asset.id}`,
      postId: canonical.post.id, assetId: canonical.asset.id,
      title: typeof canonical.post.title === 'string' ? canonical.post.title.slice(0, 10000) : '',
      publishedAt: canonical.post.publishedAt || null,
      sourceUrl: canonical.post.sourceUrl || SOURCE_PAGE,
      sourcePhotoId: photoId, path: canonical.asset.path,
      selection: manual ? 'manual' : 'candidate',
      userExcluded: item.group.some(member => member.asset.userExcluded === true),
      members: item.group.map(member => `${member.post.id}:${member.asset.id}`),
    });
  }
  return result;
}

export function publicGallery(records, { admin = false, jobStatus = null } = {}) {
  const posts = new Map();
  for (const record of records) {
    if (!validId(record.key) || !validId(record.postId) || !validId(record.assetId)) continue;
    const visible = record.status === 'published' && record.userExcluded !== true;
    if (!visible && !(admin && record.userExcluded === true)) continue;
    if (!posts.has(record.postId)) posts.set(record.postId, {
      id: record.postId, kind: 'photos', title: record.title || '', publishedAt: record.publishedAt,
      sourceUrl: record.sourceUrl, assets: [], recognition: { state: 'complete', results: [] },
    });
    const post = posts.get(record.postId);
    post.assets.push({ id: record.assetId, type: 'image', path: record.userExcluded ? null : `media/${record.objectPath}`,
      sourcePhotoId: record.sourcePhotoId, manualSelection: record.selection === 'manual', userExcluded: record.userExcluded === true });
    post.recognition.results.push({ assetId: record.assetId, state: 'candidate' });
  }
  return { version: 1, sourcePage: SOURCE_PAGE, posts: [...posts.values()], ...(admin && jobStatus ? { jobStatus } : {}) };
}
