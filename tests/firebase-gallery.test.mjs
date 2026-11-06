import assert from 'node:assert/strict';
import test from 'node:test';
import { selectPublications, publicGallery, isAdministrator } from '../cloud/gallery.mjs';

const post = {
  id: 'post-a', kind: 'photos', title: '一起玩積木', publishedAt: '2026-11-05T04:00:00Z',
  sourceUrl: 'https://www.facebook.com/100063963161862/posts/123',
  assets: [
    { id: 'a', type: 'image', path: 'media/post-a/a.jpg', sourcePhotoId: '101' },
    { id: 'b', type: 'image', path: 'media/post-a/b.jpg', sourcePhotoId: '102' },
    { id: 'c', type: 'image', path: 'media/post-a/c.jpg', sourcePhotoId: '103', userExcluded: true },
    { id: 'd', type: 'image', path: 'media/post-a/d.jpg', sourcePhotoId: '104', manualSelection: true },
  ],
  recognition: { results: [{ assetId: 'a', state: 'candidate' }, { assetId: 'b', state: 'no_match' }, { assetId: 'c', state: 'candidate' }] },
};

test('publication selection excludes rejected photos and preserves exclusion tombstones without upload eligibility', () => {
  const records = selectPublications({ version: 1, posts: [post] });
  assert.deepEqual(records.map(p => [p.sourcePhotoId, p.userExcluded]), [['101', false], ['103', true], ['104', false]]);
  assert.equal(records[0].members[0], 'post-a:a');
});

test('duplicate Facebook photos share one publication and any alias exclusion wins', () => {
  const alias = structuredClone(post);
  alias.id = 'post-b';
  alias.assets = [{ ...post.assets[0], id: 'alias', userExcluded: true }];
  alias.recognition.results = [{ assetId: 'alias', state: 'candidate' }];
  const records = selectPublications({ version: 1, posts: [post, alias] });
  assert.equal(records.filter(p => p.sourcePhotoId === '101').length, 1);
  assert.equal(records[0].userExcluded, true);
  assert.deepEqual(records[0].members, ['post-a:a', 'post-b:alias']);
});

test('public gallery publishes only ready photos and does not expose local paths or recognition scores', () => {
  const records = [
    { key: 'fb_101', postId: 'post-a', assetId: 'a', title: post.title, publishedAt: post.publishedAt, sourceUrl: post.sourceUrl, sourcePhotoId: '101', status: 'published', userExcluded: false, objectPath: 'photos/fb_101/image.jpg', imageHash: 'secret', members: ['post-a:a'] },
    { key: 'fb_103', postId: 'post-a', assetId: 'c', title: post.title, publishedAt: post.publishedAt, status: 'excluded', userExcluded: true, members: ['post-a:c'] },
  ];
  const result = publicGallery(records);
  assert.equal(result.posts.length, 1);
  assert.deepEqual(result.posts[0].assets.map(a => a.id), ['a']);
  assert.equal(result.posts[0].assets[0].path, 'media/photos/fb_101/image.jpg');
  assert.equal(JSON.stringify(result).includes('secret'), false);
  assert.equal(JSON.stringify(result).includes('photos/private.jpg'), false);
  assert.equal(publicGallery(records, { admin: true }).posts[0].assets.length, 2);
});

test('administrator access requires the allowed verified Google identity', () => {
  const valid = { email: 'eggttball@gmail.com', email_verified: true, firebase: { sign_in_provider: 'google.com' } };
  assert.equal(isAdministrator(valid, ['eggttball@gmail.com']), true);
  for (const user of [null, { ...valid, email: 'other@gmail.com' }, { ...valid, email_verified: false }, { ...valid, firebase: { sign_in_provider: 'password' } }]) {
    assert.equal(isAdministrator(user, ['eggttball@gmail.com']), false);
  }
});
