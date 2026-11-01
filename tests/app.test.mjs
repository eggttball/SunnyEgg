import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const appSource = await readFile(new URL('../public/app.js', import.meta.url), 'utf8');
const indexSource = await readFile(new URL('../public/index.html', import.meta.url), 'utf8');

// Run the real app against a small DOM fixture, without a browser dependency.
class Node {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.events = {};
    this.className = '';
    this.dataset = {};
    this.value = '';
    this.text = '';
    this.classList = { toggle: (name, enabled) => {
      const classes = new Set(this.className.split(' ').filter(Boolean));
      if (enabled) classes.add(name); else classes.delete(name);
      this.className = [...classes].join(' ');
    } };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('Raw HTML injection is not allowed'); }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.text = ''; }
  replaceWith(_) {}
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, listener) { (this.events[name] ||= []).push(listener); }
  click() { for (const listener of this.events.click || []) listener(); }
  dispatch(name) { for (const listener of this.events[name] || []) listener({ preventDefault() {} }); }
  querySelectorAll(selector) {
    const matches = node => selector.startsWith('.')
      ? node.className.split(' ').includes(selector.slice(1))
      : node.tagName === selector.toUpperCase();
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
}

function photoPost(states, extra = {}) {
  return {
    id: 'photos', kind: 'photos', title: '本機照片', publishedAt: '2026-11-01T12:00:00Z',
    sourceUrl: 'https://www.facebook.com/photo/?fbid=123',
    assets: states.map((_, index) => ({ id: `photo-${index}`, type: 'image', name: `${index}.jpg`, path: `media/photos/${index}.jpg` })),
    recognition: { state: 'complete', results: states.flatMap((state, index) => state ? [{ assetId: `photo-${index}`, state, score: state === 'candidate' ? 0.6 : null, faceCount: state === 'no_face' ? 0 : 1 }] : []) },
    clips: [], ...extra,
  };
}

const tick = () => new Promise(resolve => setImmediate(resolve));

const fetchOptions = {
  timeZone: 'Asia/Taipei', today: '2026-11-01', minDate: '2026-08-01', maxDate: '2026-11-01',
  defaultRange: { startDate: '2026-10-30', endDate: '2026-11-01' },
  presets: [
    { value: '3d', label: '最近 3 天', startDate: '2026-10-30', endDate: '2026-11-01' },
    { value: '7d', label: '最近 7 天', startDate: '2026-10-26', endDate: '2026-11-01' },
    { value: '14d', label: '最近 14 天', startDate: '2026-10-19', endDate: '2026-11-01' },
    { value: '1m', label: '最近 1 個月', startDate: '2026-10-01', endDate: '2026-11-01' },
    { value: '3m', label: '最近 3 個月', startDate: '2026-08-01', endDate: '2026-11-01' },
  ],
};

const nextDayFetchOptions = {
  timeZone: 'Asia/Taipei', today: '2026-11-02', minDate: '2026-08-02', maxDate: '2026-11-02',
  defaultRange: { startDate: '2026-10-31', endDate: '2026-11-02' },
  presets: [
    { value: '3d', label: '最近 3 天', startDate: '2026-10-31', endDate: '2026-11-02' },
    { value: '7d', label: '最近 7 天', startDate: '2026-10-27', endDate: '2026-11-02' },
    { value: '14d', label: '最近 14 天', startDate: '2026-10-20', endDate: '2026-11-02' },
    { value: '1m', label: '最近 1 個月', startDate: '2026-10-02', endDate: '2026-11-02' },
    { value: '3m', label: '最近 3 個月', startDate: '2026-08-02', endDate: '2026-11-02' },
  ],
};

function manualStatus(state, extra = {}) {
  return {
    state, range: state === 'idle' ? null : { startDate: '2026-10-30', endDate: '2026-11-01', timeZone: 'Asia/Taipei' },
    startedAt: state === 'idle' ? null : '2026-11-01T12:00:00Z',
    finishedAt: ['complete', 'partial', 'blocked', 'failed'].includes(state) ? '2026-11-01T12:01:00Z' : null,
    message: '', counts: {}, runId: state === 'idle' ? null : 'manual-1', ...extra,
  };
}

function postAudit(photoCount, extra = {}) {
  return {
    sourceId: '42_900', state: 'complete', title: '完整照片貼文',
    sourceUrl: 'https://www.facebook.com/42/posts/900', publishedAt: '2026-10-30T04:00:00Z',
    photoCount, inspectedPhotoCount: photoCount, candidatePhotoCount: 0, retainedPhotoCount: 0,
    checkedAt: '2026-11-01T12:00:00Z', checkMode: 'fresh', newInspectedPhotoCount: photoCount,
    attachments: { state: 'complete', method: 'explicit_limit_and_pagination', requestedLimit: 100, photoCount },
    ...extra,
  };
}

function auditRows(document) {
  return document.querySelector('#post-audits').querySelectorAll('tr').filter(row => row.querySelectorAll('td').length);
}

async function runApp(posts, extra = {}, manual = {}) {
  // Seed existing elements from the real page so removing a section also removes
  // its selectors here; the app cannot silently use fabricated missing elements.
  const initialNodes = [...indexSource.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*)>/gi)].map(([, tag, attributes]) => {
    const node = new Node(tag);
    for (const [, name, value] of attributes.matchAll(/([a-z][a-z0-9-]*)="([^"]*)"/gi)) {
      node.setAttribute(name, value);
      if (name === 'class') node.className = value;
      if (name === 'data-filter') node.dataset.filter = value;
    }
    node.hidden = /(?:^|\s)hidden(?:\s|$)/.test(attributes);
    return node;
  });
  const filters = initialNodes.filter(node => node.dataset.filter);
  const matches = (node, selector) => selector.startsWith('#') ? node.attributes.id === selector.slice(1)
    : selector.startsWith('.') ? node.className.split(' ').includes(selector.slice(1))
    : selector === '[data-control-only]' ? Object.hasOwn(node.attributes, 'data-control-only') : false;
  const document = {
    createElement: tag => new Node(tag),
    querySelector: selector => initialNodes.find(node => matches(node, selector)) || null,
    querySelectorAll: selector => selector === '[data-filter]' ? filters : initialNodes.filter(node => matches(node, selector)),
  };
  document.querySelector('#date-filter').value = 'all';
  const requests = [];
  const requestDetails = [];
  const timers = new Map();
  let timerId = 0;
  let status = manual.initialStatus || manualStatus('idle');
  let nextResponse = { ok: true, json: async () => ({
    version: 1, posts, sourcePage: 'https://www.facebook.com/profile.php?id=100063963161862',
    reference: { count: 7, directory: 'data/reference-child', supportedExtensions: ['.jpg'] }, ...extra,
  }) };
  const context = vm.createContext({
    document, URL, URLSearchParams, Intl, AbortController, window: { location: { search: manual.search || '' } },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    Option: function(text, value) { const node = new Node('option'); node.textContent = text; node.value = value; return node; },
    fetch: async (url, init = {}) => {
      requests.push(url);
      requestDetails.push({ url, ...init });
      if (manual.handle) {
        const response = await manual.handle(url, init);
        if (response) return response;
      }
      if (url === '/api/gallery') return nextResponse;
      if (url === '/api/manual-fetch/options') return { ok: true, json: async () => fetchOptions };
      if (url === '/api/manual-fetch/status') return { ok: true, json: async () => status };
      if (url === '/api/manual-fetch') {
        status = manualStatus('running');
        return { ok: true, status: 202, json: async () => status };
      }
      throw new Error(`Unexpected URL: ${url}`);
    },
  });
  vm.runInContext(appSource, context);
  await tick();
  assert.equal(document.querySelector('#api-error').hidden, true, 'app initialized successfully');
  return { document, filters, requests, requestDetails, content: document.querySelector('#gallery-content'),
    galleryPostIds: () => Array.from(vm.runInContext('gallery.posts.map(post => post.id)', context)),
    setManualStatus: value => { status = value; },
    poll: async () => {
      const entry = [...timers].find(([, timer]) => timer.delay === 2000);
      assert.ok(entry, 'a status reconciliation is scheduled');
      timers.delete(entry[0]);
      entry[1].callback();
      await tick();
    },
    failNextFetch: () => { nextResponse = { ok: false, status: 500, json: async () => ({ error: '相簿資料讀取失敗' }) }; } };
}

test('default gallery displays only per-photo candidate results from mixed states', async () => {
  const { content, document, filters } = await runApp([photoPost(['candidate', 'no_match', 'no_face', null])]);
  assert.deepEqual(content.querySelectorAll('img').map(node => node.src), ['/media/photos/0.jpg']);
  assert.equal(content.querySelectorAll('.recognition-message').length, 0);
  assert.equal(content.querySelectorAll('.match-label').length, 0);
  assert.equal(document.querySelector('#count-candidate').textContent, '1');
  assert.equal(document.querySelector('#count-all').textContent, '4');
  assert.equal(filters.find(button => button.dataset.filter === 'candidate').attributes['aria-pressed'], 'true');
});

test('normal gallery hides daily, manual and audit controls plus photo filter buttons', async () => {
  for (const search of ['', '?ctrl=0', '?ctrl=01', '?ctrl=true']) {
    const { document, filters, content } = await runApp([photoPost(['no_face'])], {}, { search });
    assert.equal(document.querySelector('.daily-card').hidden, true);
    assert.equal(document.querySelector('#manual-fetch-form').hidden, true);
    assert.equal(document.querySelector('.audit-section').hidden, true);
    assert.equal(filters.find(button => button.dataset.filter === 'candidate').hidden, true);
    assert.equal(filters.find(button => button.dataset.filter === 'all').hidden, true);
    assert.equal(document.querySelector('#gallery-summary'), null);
    assert.equal(document.querySelector('.sidebar-note'), null);
    assert.equal(content.querySelectorAll('button').length, 0, 'empty state does not offer the retained-photo control');
  }
});

test('ctrl=1 exposes daily, manual and audit controls plus photo filter buttons', async () => {
  const { document, filters, content } = await runApp([photoPost(['no_face'])], {}, { search: '?ctrl=1' });
  assert.equal(document.querySelector('.daily-card').hidden, false);
  assert.equal(document.querySelector('#manual-fetch-form').hidden, false);
  assert.equal(document.querySelector('.audit-section').hidden, false);
  assert.equal(filters.find(button => button.dataset.filter === 'candidate').hidden, false);
  assert.equal(filters.find(button => button.dataset.filter === 'all').hidden, false);
  assert.equal(document.querySelector('#gallery-summary'), null);
  assert.equal(document.querySelector('.sidebar-note'), null);
  const [inspect] = content.querySelectorAll('button');
  assert.equal(inspect.textContent, '查看本機保留照片');
  inspect.click();
  assert.equal(content.querySelectorAll('img').length, 1);
  assert.equal(content.querySelectorAll('.photo-feedback-button').length, 1);
});

test('removed reference section is absent while gallery initializes in either mode', async () => {
  for (const search of ['', '?ctrl=1']) {
    const { document, content } = await runApp([photoPost(['candidate'])], {}, { search });
    assert.equal(document.querySelector('#reference'), null);
    assert.equal(document.querySelector('#reference-summary'), null);
    assert.equal(content.querySelectorAll('img').length, 1);
    assert.equal(content.querySelectorAll('.recognition-message').length, 0);
    assert.equal(content.querySelectorAll('.match-label').length, 0);
  }
});

test('photo feedback buttons appear only for the exact ctrl=1 query and on every displayed image', async () => {
  for (const search of ['', '?ctrl=0', '?ctrl=01', '?ctrl=true']) {
    const { content } = await runApp([photoPost(['candidate'])], {}, { search });
    assert.equal(content.querySelectorAll('.photo-feedback-button').length, 0);
  }
  const { content, filters } = await runApp([photoPost(['candidate', 'no_match'])], {}, { search: '?ctrl=1' });
  assert.equal(content.querySelectorAll('.photo-feedback-button').length, 1);
  filters.find(button => button.dataset.filter === 'all').click();
  assert.equal(content.querySelectorAll('.photo-feedback-button').length, 2);
  for (const button of content.querySelectorAll('.photo-feedback-button')) {
    assert.equal(button.textContent, '排除');
    assert.match(button.attributes['aria-label'], /排除.*\.jpg/);
  }
});

test('normal gallery hides excluded photos from candidate and retained views while ctrl allows restoration', async () => {
  const post = photoPost(['candidate', 'no_match']);
  post.assets[0].userExcluded = true;
  const normal = await runApp([post]);
  assert.equal(normal.content.querySelectorAll('img').length, 0);
  assert.equal(normal.document.querySelector('#count-candidate').textContent, '0');
  normal.filters.find(button => button.dataset.filter === 'all').click();
  assert.deepEqual(normal.content.querySelectorAll('img').map(image => image.src), ['/media/photos/1.jpg']);
  const ctrl = await runApp([post], {}, { search: '?ctrl=1' });
  assert.equal(ctrl.content.querySelectorAll('img').length, 1);
  assert.match(ctrl.content.textContent, /已排除.*恢復照片/);
  assert.equal(ctrl.content.querySelectorAll('.photo-excluded-label').length, 1);
});

test('ctrl keeps an excluded photo available to restore after recognition changes to no-match', async () => {
  const post = photoPost(['no_match']);
  post.assets[0].userExcluded = true;
  const { content } = await runApp([post], {}, { search: '?ctrl=1' });
  assert.equal(content.querySelectorAll('img').length, 1);
  assert.match(content.querySelectorAll('.photo-feedback-button')[0].textContent, /恢復照片/);
});

test('marking a wrong photo submits its identity then refreshes without losing the selected date or view', async () => {
  const post = photoPost(['candidate', 'no_match']);
  const { content, filters, document, requestDetails } = await runApp([post], {}, {
    search: '?ctrl=1', handle: async (url, init) => {
      if (url !== '/api/photo-feedback') return;
      const body = JSON.parse(init.body);
      post.assets[0].userExcluded = body.excluded;
      return { ok: true, json: async () => ({ ...body }) };
    },
  });
  document.querySelector('#date-filter').value = '2026-11-01';
  filters.find(button => button.dataset.filter === 'all').click();
  content.querySelectorAll('.photo-feedback-button')[0].click();
  await tick();
  const request = requestDetails.find(request => request.url === '/api/photo-feedback');
  assert.equal(request.method, 'POST');
  assert.equal(request.headers['Content-Type'], 'application/json');
  assert.equal(request.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(request.body), { postId: 'photos', assetId: 'photo-0', excluded: true });
  assert.equal(document.querySelector('#date-filter').value, '2026-11-01');
  assert.equal(filters.find(button => button.dataset.filter === 'all').attributes['aria-pressed'], 'true');
  assert.match(content.textContent, /已排除.*恢復照片/);
  assert.equal(document.querySelector('#count-candidate').textContent, '0');
});

test('restoring a marked photo sends excluded=false and removes the exclusion badge', async () => {
  const post = photoPost(['candidate']);
  post.assets[0].userExcluded = true;
  const { content, requestDetails, document } = await runApp([post], {}, {
    search: '?ctrl=1', handle: async (url, init) => {
      if (url !== '/api/photo-feedback') return;
      const body = JSON.parse(init.body);
      post.assets[0].userExcluded = body.excluded;
      return { ok: true, json: async () => ({ ...body }) };
    },
  });
  content.querySelectorAll('.photo-feedback-button')[0].click();
  await tick();
  assert.deepEqual(JSON.parse(requestDetails.find(request => request.url === '/api/photo-feedback').body), { postId: 'photos', assetId: 'photo-0', excluded: false });
  assert.equal(content.querySelectorAll('.photo-excluded-label').length, 0);
  assert.equal(document.querySelector('#count-candidate').textContent, '1');
});

test('feedback requests lock repeated clicks and failures preserve the photo with a safe inline message', async () => {
  let finish;
  const { content, requests } = await runApp([photoPost(['candidate'])], {}, {
    search: '?ctrl=1', handle: async url => {
      if (url !== '/api/photo-feedback') return;
      return new Promise(resolve => { finish = resolve; });
    },
  });
  const button = content.querySelectorAll('.photo-feedback-button')[0];
  button.click();
  button.click();
  assert.equal(button.disabled, true);
  assert.equal(requests.filter(url => url === '/api/photo-feedback').length, 1);
  finish({ ok: false, json: async () => ({ error: 'private-secret /Users/private/feedback.json' }) });
  await tick();
  assert.equal(button.disabled, false);
  assert.equal(content.querySelectorAll('img').length, 1);
  assert.equal(content.querySelectorAll('.photo-excluded-label').length, 0);
  assert.match(content.textContent, /未能儲存.*重試/);
  assert.doesNotMatch(content.textContent, /private-secret|\/Users\/private/);
});

test('mismatched feedback acknowledgement never changes the displayed exclusion state', async () => {
  const { content } = await runApp([photoPost(['candidate'])], {}, {
    search: '?ctrl=1', handle: async url => url === '/api/photo-feedback'
      ? { ok: true, json: async () => ({ postId: 'another', assetId: 'photo-0', excluded: true }) } : undefined,
  });
  content.querySelectorAll('.photo-feedback-button')[0].click();
  await tick();
  assert.equal(content.querySelectorAll('.photo-excluded-label').length, 0);
  assert.match(content.textContent, /未能儲存/);
});

test('gallery collapses repeated known Facebook photo identities without hiding distinct or unknown photos', async () => {
  const post = photoPost(Array(5).fill('candidate'));
  post.assets[0].sourcePhotoId = '100';
  post.assets[1].sourcePhotoId = '100';
  post.assets[2].sourcePhotoId = '101';
  post.assets.forEach(asset => { asset.name = 'same.jpg'; });
  for (const search of ['', '?ctrl=1']) {
    const { content, document } = await runApp([post], {}, { search });
    assert.deepEqual(content.querySelectorAll('img').map(image => image.src), [
      '/media/photos/0.jpg', '/media/photos/2.jpg', '/media/photos/3.jpg', '/media/photos/4.jpg',
    ]);
    assert.equal(document.querySelector('#count-all').textContent, '4');
    assert.equal(document.querySelector('#count-candidate').textContent, '4');
  }
});

test('logical duplicate aliases cannot bypass the canonical exclusion and undo targets the retained original', async () => {
  const post = photoPost(['candidate', 'candidate']);
  post.assets[0].userExcluded = true;
  post.assets[1].duplicateOf = 'photo-0';
  const normal = await runApp([post]);
  assert.equal(normal.content.querySelectorAll('img').length, 0);
  normal.filters.find(button => button.dataset.filter === 'all').click();
  assert.equal(normal.content.querySelectorAll('img').length, 0);
  const ctrl = await runApp([post], {}, {
    search: '?ctrl=1', handle: async (url, init) => {
      if (url !== '/api/photo-feedback') return;
      const body = JSON.parse(init.body);
      post.assets[0].userExcluded = body.excluded;
      return { ok: true, json: async () => ({ ...body }) };
    },
  });
  assert.deepEqual(ctrl.content.querySelectorAll('img').map(image => image.src), ['/media/photos/0.jpg']);
  assert.equal(ctrl.content.querySelectorAll('.photo-feedback-button').length, 1);
  ctrl.content.querySelectorAll('.photo-feedback-button')[0].click();
  await tick();
  assert.deepEqual(JSON.parse(ctrl.requestDetails.find(request => request.url === '/api/photo-feedback').body), {
    postId: 'photos', assetId: 'photo-0', excluded: false,
  });
  assert.equal(ctrl.content.querySelectorAll('img').length, 1);
});

test('broken and cyclic duplicate references do not make retained photos disappear', async () => {
  const post = photoPost(['candidate', 'candidate', 'candidate']);
  post.assets[0].duplicateOf = 'missing';
  post.assets[1].duplicateOf = 'photo-2';
  post.assets[2].duplicateOf = 'photo-1';
  const { content } = await runApp([post]);
  assert.equal(content.querySelectorAll('img').length, 3);
});

test('gallery deduplicates the same Facebook photo across posts and computes logical date and total counts', async () => {
  const first = photoPost(['candidate'], { id: 'first', publishedAt: '2026-10-29T10:00:00Z' });
  const later = photoPost(['candidate', 'candidate'], { id: 'later', publishedAt: '2026-10-27T10:00:00Z' });
  first.assets[0].sourcePhotoId = '100';
  later.assets[0].sourcePhotoId = '100';
  later.assets[1].sourcePhotoId = '101';
  later.assets[0].id = 'later-0';
  later.recognition.results[0].assetId = 'later-0';
  later.assets[1].id = 'later-1';
  later.recognition.results[1].assetId = 'later-1';
  later.assets[0].path = 'media/later/0.jpg';
  later.assets[1].path = 'media/later/1.jpg';
  for (const search of ['', '?ctrl=1']) {
    const { content, document } = await runApp([first, later], {}, { search });
    assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/photos/0.jpg', '/media/later/1.jpg']);
    assert.equal(document.querySelector('#count-candidate').textContent, '2');
    document.querySelector('#date-filter').value = '2026-10-27';
    document.querySelector('#date-filter').dispatch('change');
    assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/later/1.jpg']);
    assert.equal(document.querySelector('#count-all').textContent, '1');
  }
});

test('a cross-post logical alias exclusion appears on the canonical photo and undo clears its logical group', async () => {
  const first = photoPost(['candidate'], { id: 'first' });
  const later = photoPost(['candidate'], { id: 'later' });
  first.assets[0].id = 'original';
  first.recognition.results[0].assetId = 'original';
  later.assets[0].id = 'alias';
  later.recognition.results[0].assetId = 'alias';
  later.assets[0].path = 'media/later/copy.jpg';
  later.assets[0].duplicateOf = 'original';
  later.assets[0].userExcluded = true;
  const normal = await runApp([first, later]);
  assert.equal(normal.content.querySelectorAll('img').length, 0);
  const ctrl = await runApp([first, later], {}, {
    search: '?ctrl=1', handle: async (url, init) => url === '/api/photo-feedback'
      ? { ok: true, json: async () => JSON.parse(init.body) } : undefined,
  });
  assert.deepEqual(ctrl.content.querySelectorAll('img').map(image => image.src), ['/media/photos/0.jpg']);
  assert.match(ctrl.content.textContent, /已排除.*恢復照片/);
  ctrl.content.querySelectorAll('.photo-feedback-button')[0].click();
  await tick();
  assert.deepEqual(JSON.parse(ctrl.requestDetails.find(request => request.url === '/api/photo-feedback').body), {
    postId: 'first', assetId: 'original', excluded: false,
  });
  assert.equal(ctrl.content.querySelectorAll('.photo-excluded-label').length, 0);
  assert.equal(ctrl.document.querySelector('#count-candidate').textContent, '1');
});

test('different identified photos remain visible despite an inconsistent cross-post duplicate pointer', async () => {
  const first = photoPost(['candidate'], { id: 'first' });
  const later = photoPost(['candidate'], { id: 'later' });
  first.assets[0].id = 'original';
  first.assets[0].sourcePhotoId = '100';
  first.recognition.results[0].assetId = 'original';
  later.assets[0].id = 'another';
  later.assets[0].sourcePhotoId = '101';
  later.recognition.results[0].assetId = 'another';
  later.assets[0].duplicateOf = 'original';
  later.assets[0].path = 'media/later/different.jpg';
  const { content } = await runApp([first, later]);
  assert.equal(content.querySelectorAll('img').length, 2);
});

test('conflicting alias branches cannot claim an unidentified canonical in either gallery order', async () => {
  for (const order of [[0, 1, 2], [0, 2, 1]]) {
    const original = photoPost(['candidate'], { id: 'original-post' });
    const first = photoPost(['candidate'], { id: 'first-post' });
    const second = photoPost(['candidate'], { id: 'second-post' });
    original.assets[0].id = 'original';
    original.recognition.results[0].assetId = 'original';
    original.assets[0].userExcluded = true;
    first.assets[0] = { ...first.assets[0], id: 'first', sourcePhotoId: '100', duplicateOf: 'original', path: 'media/first/1.jpg' };
    first.recognition.results[0].assetId = 'first';
    second.assets[0] = { ...second.assets[0], id: 'second', sourcePhotoId: '200', duplicateOf: 'original', path: 'media/second/2.jpg' };
    second.recognition.results[0].assetId = 'second';
    const posts = order.map(index => [original, first, second][index]);
    const normal = await runApp(posts);
    assert.deepEqual(normal.content.querySelectorAll('img').map(image => image.src), order[1] === 1
      ? ['/media/first/1.jpg', '/media/second/2.jpg'] : ['/media/second/2.jpg', '/media/first/1.jpg']);
    const ctrl = await runApp(posts, {}, { search: '?ctrl=1' });
    assert.equal(ctrl.content.querySelectorAll('img').length, 3);
    assert.equal(ctrl.content.querySelectorAll('.photo-excluded-label').length, 1);
  }
});

test('conflicting identities anywhere in a complete alias chain keep every photo independent', async () => {
  const post = photoPost(Array(3).fill('candidate'));
  post.assets[1].sourcePhotoId = '100';
  post.assets[1].duplicateOf = 'photo-0';
  post.assets[2].sourcePhotoId = '200';
  post.assets[2].duplicateOf = 'photo-1';
  const { content } = await runApp([post]);
  assert.equal(content.querySelectorAll('img').length, 3);
});

test('photo identity fallback requires aligned daily IDs and never overrides an explicit source identity', async () => {
  for (const [dailyPhotoIds, sourceId, expectedPaths] of [
    [['100', '100'], null, ['/media/photos/0.jpg']],
    [['100'], null, ['/media/photos/0.jpg', '/media/photos/1.jpg']],
    [['100', '100'], 'invalid', ['/media/photos/0.jpg', '/media/photos/1.jpg']],
    [['100', '100'], '200', ['/media/photos/0.jpg', '/media/photos/1.jpg']],
  ]) {
    const post = photoPost(['candidate', 'candidate'], { dailyPhotoIds });
    post.assets[1].sourcePhotoId = sourceId;
    const { content } = await runApp([post]);
    assert.deepEqual(content.querySelectorAll('img').map(image => image.src), expectedPaths);
  }
});

test('ambiguous asset IDs keep photos independent even when their numeric source IDs match', async () => {
  const first = photoPost(['candidate'], { id: 'first' });
  const second = photoPost(['candidate'], { id: 'second' });
  first.assets[0].sourcePhotoId = '100';
  second.assets[0].sourcePhotoId = '100';
  second.assets[0].path = 'media/second/0.jpg';
  const { content } = await runApp([first, second]);
  assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/photos/0.jpg', '/media/second/0.jpg']);
});

test('inspection toggle preserves all 12 prior photos and distinguishes no-face from no-match', async () => {
  const { content, filters, document } = await runApp([photoPost(['no_match', 'no_face', ...Array(10).fill('no_match')])]);
  assert.equal(document.querySelector('#count-all').textContent, '12');
  assert.equal(content.querySelectorAll('img').length, 0);
  filters.find(button => button.dataset.filter === 'all').click();
  assert.equal(content.querySelectorAll('img').length, 12);
  const labels = content.querySelectorAll('.match-label').map(node => node.textContent);
  assert.equal(labels.filter(label => /未偵測到人臉/.test(label)).length, 1);
  assert.equal(labels.filter(label => /人臉比對未命中/.test(label)).length, 11);
  filters.find(button => button.dataset.filter === 'candidate').click();
  assert.equal(content.querySelectorAll('img').length, 0);
});

test('all-negative default shows a candidate empty state without claiming the album is empty', async () => {
  const { content, document } = await runApp([photoPost(Array(12).fill('no_face'))]);
  assert.match(content.textContent, /沒有候選照片/);
  assert.doesNotMatch(content.textContent, /尚未加入照片|等待第一批|第一個小日常/);
  assert.equal(document.querySelector('#count-all').textContent, '12');
  assert.equal(document.querySelector('#count-candidate').textContent, '0');
  assert.equal(content.querySelectorAll('button').length, 0);
});

test('legacy videos, mixed video assets and clips never render or count as photos', async () => {
  const photo = photoPost(['candidate']);
  photo.assets.push({ id: 'stale-video', type: 'video', path: 'media/stale.mp4' });
  photo.recognition.results.push({ assetId: 'stale-video', state: 'candidate' });
  photo.clips = [{ id: 'old-clip', type: 'video', path: 'clips/stale.mp4' }];
  const videos = [
    { id: 'reel', kind: 'video', playback: 'facebook_embed', title: 'LEGACY_REEL', sourceUrl: 'https://www.facebook.com/reel/123/', assets: [] },
    { id: 'local-video', kind: 'video', title: 'LEGACY_LOCAL', assets: [{ id: 'old', type: 'video', path: 'media/old.mp4' }], recognition: { results: [{ assetId: 'old', state: 'candidate' }] } },
  ];
  const { content, document, filters, requests } = await runApp([photo, ...videos]);
  filters.find(button => button.dataset.filter === 'all').click();
  assert.equal(content.querySelectorAll('img').length, 1);
  for (const tag of ['iframe', 'video', 'script']) assert.equal(content.querySelectorAll(tag).length, 0);
  assert.equal(content.querySelectorAll('.clip-section').length, 0);
  assert.doesNotMatch(content.textContent, /LEGACY_REEL|LEGACY_LOCAL/);
  assert.equal(document.querySelector('#count-all').textContent, '1');
  assert.equal(document.querySelector('#count-candidate').textContent, '1');
  assert.deepEqual(requests, ['/api/gallery', '/api/manual-fetch/options', '/api/manual-fetch/status']);
});

test('daily status shows the Taipei schedule, simulation mode and separate last-run counts', async () => {
  const { document } = await runApp([], { jobStatus: {
    state: 'complete', simulated: true, discoveryMode: 'replay', lastRunAt: '2026-11-01T12:00:00Z',
    counts: { inspectedPhotos: 12, candidatePhotos: 0, noMatchPhotos: 8, noFacePhotos: 4 },
    message: '已完成本機模擬；未執行按讚。', schedule: { time: '20:00', timezone: 'Asia/Taipei' },
  } });
  assert.match(document.querySelector('#schedule-summary').textContent, /每天 20:00（台灣時間）/);
  const status = document.querySelector('#job-status');
  assert.equal(status.hidden, false);
  assert.match(status.textContent, /完成/);
  assert.match(status.textContent, /模擬/);
  assert.match(status.textContent, /重播/);
  assert.match(status.textContent, /本次新檢查 12 張.*候選 0 張.*人臉比對未命中 8 張.*未偵測到人臉 4 張/);
  assert.match(status.textContent, /2026.*11.*1.*20:00/);
  assert.match(status.textContent, /未執行按讚/);
});

test('daily status shows new photo likes without relabeling historical post likes', async () => {
  const { document } = await runApp([], { jobStatus: {
    state: 'complete', simulated: false, discoveryMode: 'live',
    counts: { inspectedPhotos: 12, candidatePhotos: 2, likedPhotos: 2, likedPosts: 9 },
    message: '照片檢查完成。',
  } });
  const status = document.querySelector('#job-status').textContent;
  assert.match(status, /照片新增讚 2 張/);
  assert.doesNotMatch(status, /照片新增讚 9 張/);
});

test('photo like counts omit invalid values and historical reports without photo counts', async () => {
  for (const counts of [{ likedPhotos: -1 }, { likedPhotos: 0.5 }, { likedPosts: 3 }]) {
    const { document } = await runApp([], { jobStatus: { state: 'complete', counts } });
    assert.doesNotMatch(document.querySelector('#job-status').textContent, /照片新增讚/);
  }
});

test('live blocked status remains visibly different from an empty successful gallery', async () => {
  const { document } = await runApp([], { jobStatus: {
    state: 'blocked', simulated: false, discoveryMode: 'live', counts: { inspectedPhotos: 0 },
    message: '登入失效，未完成發現照片。', lastRunAt: '2026-11-01T12:03:00Z',
  } });
  const status = document.querySelector('#job-status');
  assert.equal(status.hidden, false);
  assert.match(status.textContent, /受阻|尚未成功/);
  assert.match(status.textContent, /即時/);
  assert.match(status.textContent, /登入失效/);
});

test('full album audit reports all 98 checked photos independently of the single retained candidate', async () => {
  const { document, content, filters } = await runApp([photoPost(['candidate'])], { jobStatus: {
    state: 'partial', discoveryMode: 'live', discovery: { state: 'partial', reason: 'scan_limit' }, simulated: false, message: '已達掃描上限，日期範圍尚未完整。',
    counts: { inspectedPhotos: 98, candidatePhotos: 1, noMatchPhotos: 97, noFacePhotos: 0, processedPosts: 3, skippedPosts: 0, failedPosts: 0 },
    posts: [postAudit(51, { candidatePhotoCount: 1, retainedPhotoCount: 1 }), postAudit(24), postAudit(23)],
  } });
  assert.equal(content.querySelectorAll('img').length, 1);
  assert.equal(document.querySelector('#count-all').textContent, '1');
  assert.equal(document.querySelector('#count-candidate').textContent, '1');
  assert.match(document.querySelector('#job-status').textContent, /本次新檢查 98 張/);
  assert.match(document.querySelector('#job-status').textContent, /處理 3 篇.*失敗 0 篇/);
  const rows = auditRows(document);
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.map(row => row.querySelectorAll('td').slice(1, 5).map(cell => cell.textContent)), [
    ['51', '51', '1', '1'], ['24', '24', '0', '0'], ['23', '23', '0', '0'],
  ]);
  rows.forEach(row => assert.match(row.textContent, /本次檢查.*全部照片已比對/));
  assert.match(document.querySelector('#post-audits').textContent, /日期範圍.*尚未完整/);
  filters.find(button => button.dataset.filter === 'all').click();
  document.querySelector('#date-filter').value = 'unknown';
  document.querySelector('#date-filter').dispatch('change');
  assert.equal(auditRows(document).length, 3, 'gallery filters do not hide zero-candidate audit rows');
});

test('cached audit retains historical full album counts while reporting zero newly checked photos', async () => {
  const { document, content } = await runApp([photoPost(['candidate'])], { jobStatus: {
    state: 'complete', discoveryMode: 'live', counts: { inspectedPhotos: 0, candidatePhotos: 0, processedPosts: 0, skippedPosts: 1, failedPosts: 0 },
    posts: [postAudit(51, { checkMode: 'cached', newInspectedPhotoCount: 0, candidatePhotoCount: 1, retainedPhotoCount: 1 })],
  } });
  assert.equal(content.querySelectorAll('img').length, 1);
  assert.match(document.querySelector('#job-status').textContent, /本次新檢查 0 張/);
  assert.match(document.querySelector('#job-status').textContent, /沿用先前結果 1 篇/);
  const rows = auditRows(document);
  assert.equal(rows.length, 1);
  const [row] = rows;
  assert.deepEqual(row.querySelectorAll('td').slice(1, 5).map(cell => cell.textContent), ['51', '51', '1', '1']);
  assert.match(row.textContent, /沿用先前結果.*全部照片已比對.*2026.*11.*1.*20:00/);
});

test('audit retained counts use current logical photos including exclusions while historical checks stay intact', async () => {
  const first = photoPost(Array(3).fill('candidate'), { id: 'first' });
  first.assets[0].sourcePhotoId = '100';
  first.assets[1].sourcePhotoId = '100';
  first.assets[2].userExcluded = true;
  const later = photoPost(['candidate', 'candidate'], { id: 'later' });
  later.assets[0].id = 'copy';
  later.assets[0].duplicateOf = 'photo-0';
  later.assets[0].sourcePhotoId = '100';
  later.recognition.results[0].assetId = 'copy';
  later.assets[1].id = 'later-distinct';
  later.assets[1].sourcePhotoId = '200';
  later.recognition.results[1].assetId = 'later-distinct';
  for (const search of ['', '?ctrl=1']) {
    const { document } = await runApp([first, later], { jobStatus: {
      state: 'complete', posts: [
        postAudit(51, { galleryPostId: 'first', candidatePhotoCount: 3, retainedPhotoCount: 3 }),
        postAudit(24, { galleryPostId: 'later', candidatePhotoCount: 2, retainedPhotoCount: 2 }),
        postAudit(23, { galleryPostId: 'not-present', candidatePhotoCount: 9, retainedPhotoCount: 9 }),
        postAudit(12, { candidatePhotoCount: 7, retainedPhotoCount: 7 }),
      ],
    } }, { search });
    assert.deepEqual(auditRows(document).map(row => row.querySelectorAll('td').slice(1, 5).map(cell => cell.textContent)), [
      ['51', '51', '3', '2'], ['24', '24', '2', '1'], ['23', '23', '9', '9'], ['12', '12', '7', '7'],
    ]);
    assert.equal(document.querySelector('#count-all').textContent, search === '?ctrl=1' ? '3' : '2');
    assert.equal(document.querySelector('#count-candidate').textContent, '2');
  }
});

test('zero-candidate posts still expose completed full album checks when no photos were retained', async () => {
  const { document, content } = await runApp([], { jobStatus: {
    state: 'complete', posts: [postAudit(24)], counts: { inspectedPhotos: 24, candidatePhotos: 0 },
  } });
  assert.equal(content.querySelectorAll('img').length, 0);
  assert.match(content.textContent, /沒有候選照片/);
  const rows = auditRows(document);
  assert.equal(rows.length, 1, 'a post with no retained candidate still has a check record');
  const [row] = rows;
  assert.deepEqual(row.querySelectorAll('td').slice(1, 5).map(cell => cell.textContent), ['24', '24', '0', '0']);
  assert.match(row.textContent, /全部照片已比對/);
});

test('legacy and incomplete attachment audits never claim a verified album total or complete check', async () => {
  const { document } = await runApp([], { jobStatus: {
    state: 'partial', posts: [
      { sourceId: 'old', state: 'complete', counts: { inspectedPhotos: 12, candidatePhotos: 0 } },
      postAudit(12, { attachments: { state: 'incomplete', method: 'explicit_limit_and_pagination', requestedLimit: 100, photoCount: 12 } }),
      postAudit(24, { inspectedPhotoCount: 12 }),
      { sourceId: 'excluded', state: 'skipped', reason: 'out_of_range' },
    ],
  } });
  const rows = auditRows(document);
  assert.equal(rows.length, 3);
  const [incomplete, partial, legacy] = rows;
  assert.deepEqual(legacy.querySelectorAll('td').slice(1, 5).map(cell => cell.textContent), ['未核對', '未核對', '未核對', '未核對']);
  assert.match(legacy.textContent, /未核對/);
  assert.equal(incomplete.querySelectorAll('td')[1].textContent, '未核對');
  assert.match(incomplete.textContent, /附件尚未完整/);
  assert.match(partial.textContent, /尚未全部比對/);
  rows.forEach(row => assert.doesNotMatch(row.textContent, /全部照片已比對/));
});

test('manual status separates newly checked photos from cached and failed posts', async () => {
  const { document } = await runApp([], {}, { initialStatus: manualStatus('partial', {
    counts: { inspectedPhotos: 0, candidatePhotos: 0, processedPosts: 0, skippedPosts: 2, cachedPosts: 1, failedPosts: 1 },
  }) });
  const status = document.querySelector('#manual-fetch-status').textContent;
  assert.match(status, /本次新檢查 0 張/);
  assert.match(status, /處理 0 篇.*沿用先前結果 1 篇.*失敗 1 篇/);
});

test('gallery and audit display copies sort by full publish time with Taipei date-only midnight and stable unknown ties', async () => {
  const specifications = [
    ['unknown-first', 'invalid'],
    ['morning', '2026-11-01T01:00:00Z'],
    ['prior-night', '2026-10-31T23:30:00+08:00'],
    ['date-only', '2026-11-01'],
    ['latest-first', '2026-11-01T04:10:05Z'],
    ['missing', undefined],
    ['latest-second', '2026-11-01T12:10:05+08:00'],
    ['early-today', '2026-10-31T16:30:00Z'],
    ['previous-minute', '2026-11-01T04:09:59Z'],
    ['invalid-calendar', '2026-02-30'],
    ['yesterday', '2026-10-30T13:00:00Z'],
  ];
  const posts = specifications.map(([id, publishedAt]) => {
    const post = photoPost(['candidate'], { id, title: id, publishedAt });
    post.assets[0].id = `${id}-photo`;
    post.assets[0].path = `media/${id}/photo.jpg`;
    post.recognition.results[0].assetId = `${id}-photo`;
    return post;
  });
  const audits = specifications.map(([id, publishedAt]) => postAudit(1, { title: id, publishedAt }));
  const snapshot = JSON.stringify({ posts, audits });
  const expected = ['latest-first', 'latest-second', 'previous-minute', 'morning', 'early-today', 'date-only',
    'prior-night', 'yesterday', 'unknown-first', 'missing', 'invalid-calendar'];
  for (const search of ['', '?ctrl=1']) {
    const app = await runApp(posts, { jobStatus: { state: 'complete', posts: audits } }, { search });
    assert.deepEqual(app.content.querySelectorAll('h3').map(node => node.textContent), expected);
    assert.deepEqual(auditRows(app.document).map(row => row.querySelectorAll('.audit-title')[0].textContent), expected);
    assert.deepEqual(app.galleryPostIds(), ['unknown-first', 'morning', 'prior-night', 'date-only', 'latest-first',
      'missing', 'latest-second', 'early-today', 'previous-minute', 'invalid-calendar', 'yesterday']);
    assert.equal(JSON.stringify({ posts, audits }), snapshot, 'display sorting does not mutate source records');
    assert.equal(app.document.querySelector('#count-candidate').textContent, '11');
  }
});

test('newest-first display keeps the original duplicate owner and its date-filtered exclusion undo target', async () => {
  const old = photoPost(['candidate'], { id: 'old', title: 'old', publishedAt: '2026-10-29T10:00:00Z' });
  old.assets[0] = { ...old.assets[0], id: 'old-photo', sourcePhotoId: '100', userExcluded: true, path: 'media/old/original.jpg' };
  old.recognition.results[0].assetId = 'old-photo';
  const middle = photoPost(['candidate'], { id: 'middle', title: 'middle', publishedAt: '2026-10-31T01:00:00Z' });
  middle.assets[0] = { ...middle.assets[0], id: 'middle-photo', sourcePhotoId: '102', path: 'media/middle/photo.jpg' };
  middle.recognition.results[0].assetId = 'middle-photo';
  const latest = photoPost(['candidate', 'candidate'], { id: 'latest', title: 'latest', publishedAt: '2026-11-01T01:00:00Z' });
  latest.assets[0] = { ...latest.assets[0], id: 'latest-copy', sourcePhotoId: '100', path: 'media/latest/copy.jpg' };
  latest.assets[1] = { ...latest.assets[1], id: 'latest-distinct', sourcePhotoId: '101', path: 'media/latest/new.jpg' };
  latest.recognition.results[0].assetId = 'latest-copy';
  latest.recognition.results[1].assetId = 'latest-distinct';
  const posts = [old, middle, latest];
  const normal = await runApp(posts);
  assert.deepEqual(normal.content.querySelectorAll('img').map(image => image.src), ['/media/latest/new.jpg', '/media/middle/photo.jpg']);
  const ctrl = await runApp(posts, {}, { search: '?ctrl=1', handle: async (url, init) => url === '/api/photo-feedback'
    ? { ok: true, json: async () => JSON.parse(init.body) } : undefined });
  assert.deepEqual(ctrl.content.querySelectorAll('img').map(image => image.src), [
    '/media/latest/new.jpg', '/media/middle/photo.jpg', '/media/old/original.jpg',
  ]);
  assert.deepEqual(ctrl.galleryPostIds(), ['old', 'middle', 'latest']);
  assert.equal(ctrl.document.querySelector('#count-all').textContent, '3');
  assert.equal(ctrl.document.querySelector('#count-candidate').textContent, '2');
  assert.equal(ctrl.content.querySelectorAll('.photo-excluded-label').length, 1);
  const date = ctrl.document.querySelector('#date-filter');
  date.value = '2026-10-29';
  date.dispatch('change');
  assert.deepEqual(ctrl.content.querySelectorAll('img').map(image => image.src), ['/media/old/original.jpg']);
  ctrl.content.querySelectorAll('.photo-feedback-button')[0].click();
  await tick();
  assert.deepEqual(JSON.parse(ctrl.requestDetails.find(request => request.url === '/api/photo-feedback').body), {
    postId: 'old', assetId: 'old-photo', excluded: false,
  });
  assert.equal(date.value, '2026-10-29');
  assert.equal(ctrl.document.querySelector('#count-all').textContent, '1');
  assert.equal(ctrl.document.querySelector('#count-candidate').textContent, '1');
  date.value = 'all';
  date.dispatch('change');
  assert.deepEqual(ctrl.content.querySelectorAll('img').map(image => image.src), [
    '/media/latest/new.jpg', '/media/middle/photo.jpg', '/media/old/original.jpg',
  ]);
  assert.deepEqual(ctrl.galleryPostIds(), ['old', 'middle', 'latest']);
});

test('date filtering uses Taipei dates and changes only the visible photo set', async () => {
  const prior = photoPost(['candidate'], { id: 'prior', publishedAt: '2026-10-31T15:30:00Z' });
  const today = photoPost(['candidate', 'no_face'], { id: 'today', publishedAt: '2026-10-31T16:30:00Z' });
  const { content, document } = await runApp([prior, today]);
  const date = document.querySelector('#date-filter');
  assert.deepEqual(date.children.map(option => option.value), ['all', '2026-11-01', '2026-10-31']);
  date.value = '2026-11-01';
  date.dispatch('change');
  assert.equal(content.querySelectorAll('img').length, 1);
  assert.equal(document.querySelector('#count-all').textContent, '2');
  assert.equal(document.querySelector('#count-candidate').textContent, '1');
});

test('unprocessed photos stay out of candidates but remain available for inspection', async () => {
  const { content, filters, document } = await runApp([photoPost([null])]);
  assert.equal(content.querySelectorAll('img').length, 0);
  assert.equal(document.querySelector('#count-all').textContent, '1');
  assert.equal(document.querySelector('#count-candidate').textContent, '0');
  filters.find(button => button.dataset.filter === 'all').click();
  assert.equal(content.querySelectorAll('img').length, 1);
  assert.match(content.textContent, /尚未產生辨識結果/);
});

test('malformed local image paths and unsafe source links are never loaded', async () => {
  const post = photoPost(['candidate'], { sourceUrl: 'https://user:secret@www.facebook.com/photo/?fbid=123' });
  post.assets[0].path = 'media/../reference-child/child.jpg';
  const { content } = await runApp([post]);
  assert.equal(content.querySelectorAll('img').length, 0);
  assert.equal(content.querySelectorAll('a').length, 0);
  assert.match(content.textContent, /無法顯示/);
  assert.doesNotMatch(content.textContent, /secret/);
});

test('read failures keep the last successful candidate gallery and display the error', async () => {
  const { content, document, failNextFetch } = await runApp([photoPost(['candidate'])]);
  failNextFetch();
  document.querySelector('#refresh').click();
  await tick();
  assert.equal(content.querySelectorAll('img').length, 1);
  assert.equal(document.querySelector('#api-error').hidden, false);
  assert.match(document.querySelector('#api-error').textContent, /保留上次成功讀取/);
  assert.equal(document.querySelector('#refresh').disabled, false);
});

test('manual fetch defaults to server Taipei dates independently from the gallery filter', async () => {
  const { document } = await runApp([photoPost(['candidate'])]);
  assert.equal(document.querySelector('#fetch-preset').value, '3d');
  assert.equal(document.querySelector('#fetch-start-date').value, '2026-10-30');
  assert.equal(document.querySelector('#fetch-end-date').value, '2026-11-01');
  for (const selector of ['#fetch-start-date', '#fetch-end-date']) {
    assert.equal(document.querySelector(selector).min, '2026-08-01');
    assert.equal(document.querySelector(selector).max, '2026-11-01');
  }
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
  document.querySelector('#date-filter').value = '2026-11-01';
  document.querySelector('#date-filter').dispatch('change');
  assert.equal(document.querySelector('#fetch-start-date').value, '2026-10-30');
  assert.match(document.querySelector('#fetch-range-summary').textContent, /2026-10-30.*2026-11-01.*台灣/);
});

test('preset and custom fetch dates send one explicit inclusive range while busy blocks duplicates', async () => {
  let accept;
  const app = await runApp([], {}, { handle: async (url, init) => {
    if (url === '/api/manual-fetch') return new Promise(resolve => { accept = resolve; });
  } });
  const { document, requestDetails } = app;
  const preset = document.querySelector('#fetch-preset');
  preset.value = '1m';
  preset.dispatch('change');
  assert.equal(document.querySelector('#fetch-start-date').value, '2026-10-01');
  const start = document.querySelector('#fetch-start-date');
  start.value = '2026-10-05';
  start.dispatch('change');
  assert.equal(preset.value, 'custom');
  const form = document.querySelector('#manual-fetch-form');
  form.dispatch('submit');
  form.dispatch('submit');
  await tick();
  const [request] = requestDetails.filter(request => request.url === '/api/manual-fetch');
  assert.deepEqual(JSON.parse(request.body), { startDate: '2026-10-05', endDate: '2026-11-01' });
  assert.equal(request.method, 'POST');
  assert.equal(request.headers['Content-Type'], 'application/json');
  assert.equal(requestDetails.filter(request => request.url === '/api/manual-fetch').length, 1);
  for (const selector of ['#fetch-preset', '#fetch-start-date', '#fetch-end-date', '#manual-fetch-submit']) {
    assert.equal(document.querySelector(selector).disabled, true);
  }
  accept({ ok: true, status: 202, json: async () => manualStatus('running') });
  await tick();
  assert.match(document.querySelector('#manual-fetch-status').textContent, /進行中/);
});

test('reloading a page recovers an active fetch and refreshes the gallery once it completes', async () => {
  const { document, requests, setManualStatus, poll } = await runApp([], {}, { initialStatus: manualStatus('running') });
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /進行中/);
  setManualStatus(manualStatus('complete', { counts: { inspectedPhotos: 12, candidatePhotos: 2, likedPhotos: 1 } }));
  await poll();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /已完成.*本次新檢查 12 張.*候選 2 張/);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /照片新增讚 1 張/);
  assert.equal(requests.filter(url => url === '/api/gallery').length, 2);
});

for (const [state, label] of [['blocked', /受阻/], ['failed', /失敗/], ['partial', /部分完成/]]) {
  test(`manual ${state} status stays truthful and refreshes retained photos`, async () => {
    const { document, content, requests, setManualStatus, poll } = await runApp([photoPost(['candidate'])], {}, { initialStatus: manualStatus('running') });
    setManualStatus(manualStatus(state, { message: '請確認本機服務狀態。' }));
    await poll();
    const status = document.querySelector('#manual-fetch-status');
    assert.match(status.textContent, label);
    assert.doesNotMatch(status.textContent, /%|已檢查 0 張|抓取已完成/);
    assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
    assert.equal(content.querySelectorAll('img').length, 1);
    assert.equal(requests.filter(url => url === '/api/gallery').length, 2);
  });
}

test('lost POST response preserves the lock until status reconciles without exposing the error', async () => {
  const { document, setManualStatus, poll, requests } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch') throw new Error('SECRET_TOKEN /private/session.json');
  } });
  document.querySelector('#manual-fetch-form').dispatch('submit');
  await tick();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  const status = document.querySelector('#manual-fetch-status');
  assert.match(status.textContent, /確認|連線/);
  assert.doesNotMatch(status.textContent, /SECRET_TOKEN|private|抓取失敗/);
  document.querySelector('#manual-fetch-form').dispatch('submit');
  assert.equal(requests.filter(url => url === '/api/manual-fetch').length, 1);
  setManualStatus(manualStatus('running'));
  await poll();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  setManualStatus(manualStatus('complete'));
  await poll();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
});

test('status network failures keep a running fetch locked and polling', async () => {
  let unavailable = false;
  const { document, poll, setManualStatus } = await runApp([], {}, {
    initialStatus: manualStatus('running'),
    handle: async url => { if (url === '/api/manual-fetch/status' && unavailable) throw new Error('network detail'); },
  });
  unavailable = true;
  await poll();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /確認|連線/);
  unavailable = false;
  setManualStatus(manualStatus('idle'));
  await poll();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
});

test('a conflict adopts the server active run instead of permitting another fetch', async () => {
  const { document, poll, setManualStatus } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch') return { ok: false, status: 409, json: async () => ({ error: 'already running', status: manualStatus('running') }) };
  } });
  document.querySelector('#manual-fetch-form').dispatch('submit');
  await tick();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /進行中/);
  setManualStatus(manualStatus('partial'));
  await poll();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
});

test('invalid date order and server validation errors show safe messages without retrying POST', async () => {
  const { document, requests } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch') return { ok: false, status: 400, json: async () => ({ error: '/private/session.json SECRET' }) };
  } });
  const start = document.querySelector('#fetch-start-date');
  const end = document.querySelector('#fetch-end-date');
  start.value = '2026-11-01';
  end.value = '2026-10-30';
  start.dispatch('change');
  document.querySelector('#manual-fetch-form').dispatch('submit');
  await tick();
  assert.equal(requests.filter(url => url === '/api/manual-fetch').length, 0);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /日期|起日/);
  start.value = '2026-10-30';
  end.value = '2026-11-01';
  start.dispatch('change');
  document.querySelector('#manual-fetch-form').dispatch('submit');
  await tick();
  assert.equal(requests.filter(url => url === '/api/manual-fetch').length, 1);
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
  assert.doesNotMatch(document.querySelector('#manual-fetch-status').textContent, /private|SECRET/);
});

test('unavailable fetch options remain explained even when idle status arrives later', async () => {
  let respondStatus;
  const { document } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch/options') throw new Error('PRIVATE_CONFIGURATION');
    if (url === '/api/manual-fetch/status') return new Promise(resolve => { respondStatus = resolve; });
  } });
  respondStatus({ ok: true, json: async () => manualStatus('idle') });
  await tick();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  assert.equal(document.querySelector('#manual-fetch-status').hidden, false);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /設定.*重新整理/);
  assert.doesNotMatch(document.querySelector('#manual-fetch-status').textContent, /PRIVATE_CONFIGURATION/);
});

test('unavailable fetch options are explained when the initial idle status arrives first', async () => {
  let rejectOptions;
  const { document } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch/options') return new Promise((resolve, reject) => { rejectOptions = reject; });
  } });
  rejectOptions(new Error('PRIVATE_CONFIGURATION'));
  await tick();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  assert.equal(document.querySelector('#manual-fetch-status').hidden, false);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /設定.*重新整理/);
});

test('an unavailable initial status keeps controls disabled until idle is confirmed', async () => {
  let failStatus = true;
  const { document, poll } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch/status' && failStatus) throw new Error('PRIVATE_STATUS');
  } });
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /確認|連線/);
  failStatus = false;
  await poll();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
});

test('a preset fetch opened across midnight refreshes server dates before a single POST', async () => {
  let optionsReads = 0;
  let respondOptions;
  const { document, requestDetails } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch/options' && ++optionsReads > 1) return new Promise(resolve => { respondOptions = resolve; });
  } });
  const form = document.querySelector('#manual-fetch-form');
  form.dispatch('submit');
  form.dispatch('submit');
  await tick();
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, true);
  assert.equal(requestDetails.filter(request => request.url === '/api/manual-fetch').length, 0);
  assert.equal(optionsReads, 2);
  respondOptions({ ok: true, json: async () => nextDayFetchOptions });
  await tick();
  const posts = requestDetails.filter(request => request.url === '/api/manual-fetch');
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0].body), { startDate: '2026-10-31', endDate: '2026-11-02' });
  assert.equal(document.querySelector('#fetch-start-date').value, '2026-10-31');
  assert.equal(document.querySelector('#fetch-end-date').value, '2026-11-02');
  assert.equal(document.querySelector('#fetch-start-date').min, '2026-08-02');
  assert.equal(document.querySelector('#fetch-end-date').max, '2026-11-02');
  assert.match(document.querySelector('#fetch-range-summary').textContent, /2026-10-31.*2026-11-02/);
});

test('refreshing options before a custom fetch preserves the chosen inclusive dates', async () => {
  let optionsReads = 0;
  const { document, requestDetails } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch/options' && ++optionsReads > 1) return { ok: true, json: async () => nextDayFetchOptions };
  } });
  document.querySelector('#fetch-start-date').value = '2026-10-05';
  document.querySelector('#fetch-end-date').value = '2026-10-20';
  document.querySelector('#fetch-start-date').dispatch('change');
  document.querySelector('#manual-fetch-form').dispatch('submit');
  await tick();
  assert.equal(optionsReads, 2);
  assert.equal(document.querySelector('#fetch-preset').value, 'custom');
  const [request] = requestDetails.filter(request => request.url === '/api/manual-fetch');
  assert.deepEqual(JSON.parse(request.body), { startDate: '2026-10-05', endDate: '2026-10-20' });
  assert.equal(document.querySelector('#fetch-end-date').max, '2026-11-02');
});

test('fresh options reject a custom date that has passed the oldest allowed day', async () => {
  let optionsReads = 0;
  const { document, requests } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch/options' && ++optionsReads > 1) return { ok: true, json: async () => nextDayFetchOptions };
  } });
  document.querySelector('#fetch-start-date').value = '2026-08-01';
  document.querySelector('#fetch-start-date').dispatch('change');
  document.querySelector('#manual-fetch-form').dispatch('submit');
  await tick();
  assert.equal(requests.filter(url => url === '/api/manual-fetch').length, 0);
  assert.equal(document.querySelector('#fetch-start-date').value, '2026-08-01');
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
  assert.match(document.querySelector('#manual-fetch-status').textContent, /日期.*3 個月/);
});

test('a failed options refresh before POST unlocks safely and allows a later retry', async () => {
  let optionsReads = 0;
  const { document, requests } = await runApp([], {}, { handle: async url => {
    if (url === '/api/manual-fetch/options' && ++optionsReads === 2) throw new Error('PRIVATE_OPTIONS_TOKEN');
  } });
  document.querySelector('#manual-fetch-form').dispatch('submit');
  await tick();
  assert.equal(requests.filter(url => url === '/api/manual-fetch').length, 0);
  assert.equal(document.querySelector('#manual-fetch-submit').disabled, false);
  const status = document.querySelector('#manual-fetch-status');
  assert.match(status.textContent, /日期|設定/);
  assert.match(status.textContent, /再試|重試/);
  assert.doesNotMatch(status.textContent, /PRIVATE_OPTIONS_TOKEN|進行中/);
  document.querySelector('#manual-fetch-form').dispatch('submit');
  await tick();
  assert.equal(requests.filter(url => url === '/api/manual-fetch').length, 1);
});

function galleryResponse(title, path) {
  return { ok: true, json: async () => ({
    version: 1, sourcePage: 'https://www.facebook.com/profile.php?id=100063963161862',
    reference: { count: 7, directory: 'data/reference-child', supportedExtensions: ['.jpg'] },
    posts: [photoPost(['candidate'], { title, assets: [{ id: 'photo-0', type: 'image', name: '0.jpg', path }] })],
  }) };
}

test('a terminal status received during initialization refreshes an earlier gallery snapshot', async () => {
  let finishStatus;
  let galleryReads = 0;
  const { content, requests } = await runApp([], {}, { handle: async url => {
    if (url === '/api/gallery') return ++galleryReads === 1 ? galleryResponse('舊照片', 'media/old/0.jpg') : galleryResponse('新照片', 'media/new/0.jpg');
    if (url === '/api/manual-fetch/status') return new Promise(resolve => { finishStatus = resolve; });
  } });
  assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/old/0.jpg']);
  finishStatus({ ok: true, json: async () => manualStatus('complete') });
  await tick();
  assert.equal(requests.filter(url => url === '/api/gallery').length, 2);
  assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/new/0.jpg']);
});

test('an old gallery response arriving after terminal refresh cannot overwrite the fresh photos', async () => {
  let finishOldGallery;
  let finishStatus;
  let galleryReads = 0;
  const { content, requests } = await runApp([], {}, { handle: async url => {
    if (url === '/api/gallery') {
      if (++galleryReads === 1) return new Promise(resolve => { finishOldGallery = resolve; });
      return galleryResponse('新照片', 'media/new/0.jpg');
    }
    if (url === '/api/manual-fetch/status') return new Promise(resolve => { finishStatus = resolve; });
  } });
  finishStatus({ ok: true, json: async () => manualStatus('complete') });
  await tick();
  assert.equal(requests.filter(url => url === '/api/gallery').length, 2);
  assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/new/0.jpg']);
  finishOldGallery(galleryResponse('舊照片', 'media/old/0.jpg'));
  await tick();
  assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/new/0.jpg']);
});

test('an older gallery completion keeps refresh controls busy until the newest request finishes', async () => {
  const pending = [];
  const { document, content, poll, setManualStatus } = await runApp([], {}, {
    initialStatus: manualStatus('running'),
    handle: async url => { if (url === '/api/gallery') return new Promise(resolve => pending.push(resolve)); },
  });
  setManualStatus(manualStatus('complete'));
  await poll();
  assert.equal(pending.length, 2);
  pending[0](galleryResponse('舊照片', 'media/old/0.jpg'));
  await tick();
  assert.equal(document.querySelector('#refresh').disabled, true);
  assert.equal(content.attributes['aria-busy'], 'true');
  pending[1](galleryResponse('新照片', 'media/new/0.jpg'));
  await tick();
  assert.equal(document.querySelector('#refresh').disabled, false);
  assert.equal(content.attributes['aria-busy'], 'false');
  assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/new/0.jpg']);
});

test('an older failed gallery request cannot replace a newer successful gallery with an error', async () => {
  let failOldGallery;
  let galleryReads = 0;
  const { document, content, poll, setManualStatus } = await runApp([], {}, {
    initialStatus: manualStatus('running'),
    handle: async url => {
      if (url === '/api/gallery') {
        if (++galleryReads === 1) return new Promise((resolve, reject) => { failOldGallery = reject; });
        return galleryResponse('新照片', 'media/new/0.jpg');
      }
    },
  });
  setManualStatus(manualStatus('complete'));
  await poll();
  failOldGallery(new Error('Old request failed'));
  await tick();
  assert.equal(document.querySelector('#api-error').hidden, true);
  assert.deepEqual(content.querySelectorAll('img').map(image => image.src), ['/media/new/0.jpg']);
});
