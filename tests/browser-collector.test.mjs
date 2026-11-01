import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { runInNewContext } from 'node:vm';
import * as collector from '../scripts/browser-collector.mjs';
import { normalizePhotoLink, collectObservations, classifyPage } from '../scripts/browser-collector.mjs';

test('public photo IDs retained while tracking parameters are removed', () => {
  assert.deepEqual(normalizePhotoLink('https://www.facebook.com/photo/?fbid=123&set=pcb.456&__cft__[0]=tracking'), {
    photoId: '123', albumPostId: '456', sourceUrl: 'https://www.facebook.com/photo/?fbid=123&set=pcb.456',
  });
  assert.deepEqual(normalizePhotoLink('https://www.facebook.com/photo/?fbid=123&set=a.456'), {photoId:'123',sourceUrl:'https://www.facebook.com/photo/?fbid=123'});
  assert.deepEqual(normalizePhotoLink('https://www.facebook.com/photo.php?fbid=124'), {photoId:'124',sourceUrl:'https://www.facebook.com/photo/?fbid=124'});
});

test('secret, external, malformed IDs and userinfo URLs are rejected', () => {
  for (const value of ['https://evil.example/photo/?fbid=123&set=pcb.456', 'https://www.facebook.com/photo/?fbid=nope&set=pcb.456', 'https://www.facebook.com/photo/?fbid=123&set=pcb.456&access_token=secret', 'https://user@www.facebook.com/photo/?fbid=123&set=pcb.456']) {
    assert.equal(normalizePhotoLink(value), null);
  }
});

test('one discovery per photo post excludes featured, pinned and video cards', () => {
  const observations = [
    { href:'https://www.facebook.com/photo/?fbid=123&set=pcb.456' },
    { href:'https://www.facebook.com/photo/?fbid=124&set=pcb.456' },
    { href:'https://www.facebook.com/photo/?fbid=125&set=pcb.457', pinned:true },
    { href:'https://www.facebook.com/photo/?fbid=126&set=pcb.458', featured:true },
    { href:'https://www.facebook.com/reel/123/', video:true },
    { href:'https://www.facebook.com/photo/?fbid=127&set=pcb.459', video:true },
    { href:'https://www.facebook.com/photo/?fbid=128&set=a.460', inPost:false },
    { href:'https://www.facebook.com/photo/?fbid=129', inPost:true },
  ];
  const result = collectObservations(observations);
  assert.equal(result.posts.length, 2);
  assert.equal(result.posts[0].photoId, '123');
  assert.equal(result.skippedVideos, 2);
});

test('login walls and broken pages are distinct from an empty feed', () => {
  assert.equal(classifyPage({authenticated:false,loginForm:true,postSection:false,unavailable:false}), 'login_required');
  assert.equal(classifyPage({authenticated:true,loginForm:false,postSection:false,unavailable:true}), 'page_unavailable');
  assert.equal(classifyPage({authenticated:true,loginForm:false,postSection:true,unavailable:false}), null);
  assert.equal(classifyPage({authenticated:true,loginForm:false,postSection:false,unavailable:false}), 'feed_unavailable');
  assert.equal(classifyPage({authenticated:false,loginForm:false,postSection:true,unavailable:false}), 'login_required');
});

async function profileFixture(t) {
  const profile = await mkdtemp(join(tmpdir(), 'sunnyegg-collector-'));
  t.after(() => rm(profile, { recursive:true, force:true }));
  return profile;
}

function browserModule(launch, executable = process.execPath) {
  return async () => ({ chromium:{ executablePath:() => executable, launchPersistentContext:launch } });
}

function discoveryContext({ height=9000, viewport=1000, nested=false, locked=false,
  photos=({top}) => [String(100 + Math.floor(top / 900))], onWait=() => {} } = {}) {
  const model = { top:0, height, elapsed:0 };
  let closeCalls = 0;
  const root = { parentElement:null, overflowY:'visible' };
  const container = nested ? { parentElement:root, overflowY:'auto' } : root;
  const feed = { parentElement:container };
  const contains = function(node) { for (;node;node=node.parentElement) if(node===this) return true; return false; };
  for (const node of [root, container, feed]) node.contains = contains;
  for (const node of new Set([root, container])) {
    Object.defineProperties(node, {
      scrollTop:{get:() => node===container ? model.top : 0},
      scrollHeight:{get:() => node===container ? model.height : viewport},
      clientHeight:{get:() => viewport},
    });
    node.scrollBy = (x,y) => { if(!locked && node===container) model.top=Math.min(Math.max(0,model.height-viewport),model.top+y); };
  }
  const page = {
    goto:async () => {},
    getByRole:() => ({first:() => ({waitFor:async () => {}})}),
    waitForTimeout:async milliseconds => { model.elapsed+=milliseconds; onWait(model); },
    evaluate:async (callback, argument) => {
      const anchors = photos(model).map(id => {
        const card = { parentElement:feed, querySelector:() => null, querySelectorAll:() => [] };
        return { href:`https://www.facebook.com/photo/?fbid=${id}&set=pcb.${id}`, parentElement:card,
          closest:selector => selector.includes('article') ? card : null };
      });
      const heading = {textContent:'貼文',parentElement:feed,closest:() => null};
      const document = {
        documentElement:root, scrollingElement:root, body:{innerText:'貼文'},
        querySelectorAll:selector => selector==='a[href]' ? anchors : [heading],
        querySelector:selector => selector==='[role="feed"]' ? feed : selector.startsWith('[aria-label') ? {} : null,
      };
      const window = {innerHeight:viewport,scrollBy:(x,y) => root.scrollBy(x,y)};
      return runInNewContext(`(${callback.toString()})(argument)`, {
        document,window,argument,getComputedStyle:node => ({overflowY:node.overflowY || 'visible'}),
      });
    },
  };
  return { context:{pages:() => [page],close:async () => {closeCalls++;}},closeCalls:() => closeCalls };
}

async function discoverWithFixture(t, fixture, maxScrolls=12) {
  const profile = await profileFixture(t);
  return run(['discover','--profile',profile,'--max-scrolls',String(maxScrolls)], {
    loadPlaywright:browserModule(() => fixture.context),
  });
}

test('discovery traverses unchanged document height before the bottom', async t => {
  const fixture = discoveryContext();
  const result = await discoverWithFixture(t,fixture);
  assert.ok(result.posts.some(post => post.photoId==='108'), 'the post near the bottom is discovered');
  assert.equal(result.state,'partial');
  assert.equal(result.reason,'feed_stalled');
  assert.equal(result.coverage.atBottom,true);
  assert.equal(fixture.closeCalls(),1);
});

test('discovery scrolls the nested feed container instead of the window', async t => {
  const result = await discoverWithFixture(t,discoveryContext({nested:true}));
  assert.ok(result.posts.some(post => post.photoId==='108'));
  assert.equal(result.coverage.scroll.container,'element');
  assert.equal(result.coverage.atBottom,true);
});

test('discovery waits for delayed bottom loading and retains virtualized post IDs', async t => {
  const fixture = discoveryContext({height:1000,photos:({top}) => [top===0 ? '100' : '200'],
    onWait:model => { if(model.elapsed>=2000) model.height=2800; }});
  const result = await discoverWithFixture(t,fixture);
  assert.deepEqual(result.posts.map(post => post.photoId),['100','200']);
  assert.equal(result.coverage.observedPhotoPosts,2);
  assert.equal(result.coverage.atBottom,true);
  assert.equal(result.reason,'feed_stalled');
});

test('discovery resumes bottom loading when new IDs appear without height growth', async t => {
  const fixture = discoveryContext({height:1000,
    photos:({elapsed}) => [elapsed>=4500 ? '200' : elapsed>=2000 ? '150' : '100']});
  const result = await discoverWithFixture(t,fixture);
  assert.deepEqual(result.posts.map(post => post.photoId),['100','150','200']);
  assert.equal(result.coverage.scroll.height,1000);
  assert.equal(result.coverage.observedPhotoPosts,3);
  assert.equal(result.reason,'feed_stalled');
});

test('discovery captures the batch loaded after the last allowed scroll', async t => {
  const result = await discoverWithFixture(t,discoveryContext({photos:({top}) => [top===0 ? '100' : '200']}),1);
  assert.deepEqual(result.posts.map(post => post.photoId),['100','200']);
  assert.equal(result.reason,'scan_limit');
  assert.equal(result.coverage.scrolls,1);
  assert.equal(result.coverage.atBottom,false);
});

test('discovery reports genuine bottom inactivity as partial coverage', async t => {
  const fixture = discoveryContext({height:1000,photos:() => ['100']});
  const result = await discoverWithFixture(t,fixture,100);
  assert.equal(result.state,'partial');
  assert.equal(result.reason,'feed_stalled');
  assert.equal(result.coverage.state,'partial');
  assert.equal(result.coverage.atBottom,true);
  assert.ok(result.coverage.loadWaitMs>=2000, 'bottom inactivity receives a load grace period');
  assert.ok(result.coverage.scrolls<100, 'stalled feed does not use the whole scan budget');
});

test('discovery bounds a feed whose scroll position cannot advance', async t => {
  const result = await discoverWithFixture(t,discoveryContext({locked:true}),4);
  assert.equal(result.state,'partial');
  assert.equal(result.reason,'scan_limit');
  assert.equal(result.coverage.atBottom,false);
  assert.equal(result.coverage.scrolls,4);
});

const POST_URL = 'https://www.facebook.com/42/posts/900/';
const POST_LIKE = '對小鹿托嬰中心的貼文表示讚';
const POST_REMOVE = '從小鹿托嬰中心的貼文移除讚';
const COMMENT_REMOVE = '對留言收回讚';

function reactionContext({ labels=[], afterClick=[], hydrateLabels, hydrateDelay=0 } = {}) {
  let buttons = [...labels], clicks = 0, closeCalls = 0, hydration;
  const locatorFor = (names, first=false) => {
    const matches = () => buttons.filter(label => names.some(name => name.test(label)));
    const locator = {
      first:() => locatorFor(names, true),
      or:other => locatorFor([...names, ...other.names]),
      names,
      count:async () => first ? Math.min(matches().length, 1) : matches().length,
      waitFor:async () => {
        const deadline = Date.now() + 40;
        while (!matches().length && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 1));
        if (!matches().length || (!first && matches().length > 1)) throw new Error('fixture reaction control unavailable');
      },
      click:async () => {
        if (!matches().length || (!first && matches().length > 1)) throw new Error('fixture reaction control unavailable');
        clicks++;
        buttons = [...afterClick];
      },
    };
    return locator;
  };
  const page = {
    goto:async url => {
      assert.equal(url, POST_URL);
      if (hydrateLabels) hydration = setTimeout(() => { buttons = [...hydrateLabels]; }, hydrateDelay);
    },
    getByRole:(role, {name}) => {
      assert.equal(role, 'button');
      return locatorFor([name]);
    },
  };
  return {
    context:{ pages:() => [page], close:async () => { clearTimeout(hydration); closeCalls++; } },
    clicks:() => clicks,
    closeCalls:() => closeCalls,
  };
}

async function reactWithFixture(t, fixture) {
  const profile = await profileFixture(t);
  return run(['react','--profile',profile,'--url',POST_URL], {
    loadPlaywright:browserModule(() => fixture.context),
  });
}

test('reaction verifies the observed author-specific remove label after clicking', async t => {
  const fixture = reactionContext({ labels:[POST_LIKE], afterClick:[POST_REMOVE] });
  const result = await reactWithFixture(t, fixture);
  assert.equal(result.state, 'complete');
  assert.equal(result.reaction, 'liked');
  assert.equal(fixture.clicks(), 1);
  assert.equal(fixture.closeCalls(), 1);
});

test('reaction recognizes the observed already-liked state without another click', async t => {
  const fixture = reactionContext({ labels:[POST_REMOVE] });
  const result = await reactWithFixture(t, fixture);
  assert.equal(result.state, 'complete');
  assert.equal(result.reaction, 'already_liked');
  assert.equal(fixture.clicks(), 0);
});

test('reaction retains the author-specific legacy unlike state alongside comment controls', async t => {
  const fixture = reactionContext({ labels:['對小鹿托嬰中心的貼文收回讚',COMMENT_REMOVE] });
  const result = await reactWithFixture(t, fixture);
  assert.equal(result.state, 'complete');
  assert.equal(result.reaction, 'already_liked');
  assert.equal(fixture.clicks(), 0);
});

test('reaction waits for a hydrated already-liked control before deciding', async t => {
  const fixture = reactionContext({ hydrateLabels:[POST_REMOVE], hydrateDelay:5 });
  const result = await reactWithFixture(t, fixture);
  assert.equal(result.state, 'complete');
  assert.equal(result.reaction, 'already_liked');
  assert.equal(fixture.clicks(), 0);
});

test('reaction never treats a comment unlike control as an already-liked post', async t => {
  const fixture = reactionContext({ labels:[COMMENT_REMOVE] });
  const result = await reactWithFixture(t, fixture);
  assertBlocked(result, 'browser_unavailable');
  assert.equal(fixture.clicks(), 0);
});

test('reaction never verifies a post click using a comment unlike control', async t => {
  const fixture = reactionContext({ labels:[POST_LIKE], afterClick:[POST_LIKE,COMMENT_REMOVE] });
  const result = await reactWithFixture(t, fixture);
  assertBlocked(result, 'browser_unavailable');
  assert.equal(fixture.clicks(), 1);
});

test('reaction fails closed when author-specific reaction controls are ambiguous', async t => {
  const fixture = reactionContext({ labels:['對小鹿托嬰中心的貼文收回讚','對小鹿托嬰中心的貼文收回讚'] });
  const result = await reactWithFixture(t, fixture);
  assertBlocked(result, 'browser_unavailable');
  assert.equal(fixture.clicks(), 0);
});

const PHOTO_ID = '700';
const PHOTO_URL = 'https://www.facebook.com/photo/?fbid=700';
const PHOTO_FEEDBACK = 'ZmVlZGJhY2s6NzAz';
const LIKE_REACTION = '1635855486666999';

function photoReactionContext({ labels=[POST_LIKE], currentUrl=PHOTO_URL,
  feedbackUrl=PHOTO_URL, feedbackId=PHOTO_FEEDBACK, mutationFeedback=PHOTO_FEEDBACK,
  mutationReaction=LIKE_REACTION, afterClick=[POST_REMOVE], afterClickUrl,
  extraPhoto, missingFeedback=false, responseOk=true, responseDelay=0, persistedLabels } = {}) {
  let buttons=[...labels], url=currentUrl, clicks=0, accepted=0, aborted=0, closed=0, handler,
    responseFinished=false, closedBeforeResponse=false,navigations=0;
  const bootstrap={ data:{ photo:{ __typename:'Photo', id:PHOTO_ID,
    feedback:missingFeedback ? undefined : { __typename:'Feedback', id:feedbackId,
      url:feedbackUrl, can_viewer_react:true, viewer_feedback_reaction_info:null } }, extraPhoto } };
  const locatorFor=names=>({
    names,
    or:other=>locatorFor([...names,...other.names]),
    count:async()=>buttons.filter(label=>names.some(name=>name.test(label))).length,
    waitFor:async()=>{
      const matches=buttons.filter(label=>names.some(name=>name.test(label)));
      if(matches.length!==1) throw new Error('fixture control unavailable');
    },
    click:async()=>{
      clicks++;
      const form=new URLSearchParams({fb_api_req_friendly_name:'CometUFIFeedbackReactMutation',
        variables:JSON.stringify({input:{feedback_id:mutationFeedback,feedback_reaction_id:mutationReaction}})});
      const route={ request:()=>({method:()=> 'POST',postData:()=>form.toString(),response:async()=>{
        if(responseDelay) await new Promise(resolve=>setTimeout(resolve,responseDelay));
        return {ok:()=>responseOk,finished:async()=>{responseFinished=true;return null;}};
      }}),
        continue:async()=>{accepted++;buttons=[...afterClick];if(afterClickUrl)url=afterClickUrl;},
        abort:async()=>{aborted++;} };
      if(handler) await handler(route);
      else await route.continue();
    },
  });
  const page={
    goto:async destination=>{
      assert.equal(destination,PHOTO_URL);
      navigations++;
      if(navigations>1) {url=PHOTO_URL;buttons=[...(persistedLabels || afterClick)];}
    },
    url:()=>url,
    getByRole:(role,{name})=>{assert.equal(role,'button');return locatorFor([name]);},
    evaluate:async(callback,argument)=>runInNewContext('(' + callback.toString() + ')(argument)',{
      argument,URL,document:{querySelectorAll:()=>[{textContent:JSON.stringify(bootstrap)}]},
    }),
    route:async(pattern,callback)=>{handler=callback;},
    unroute:async()=>{handler=undefined;},
  };
  return {context:{pages:()=>[page],close:async()=>{closed++;closedBeforeResponse=accepted>0 && !responseFinished;}},
    clicks:()=>clicks,accepted:()=>accepted,aborted:()=>aborted,closed:()=>closed,
    closedBeforeResponse:()=>closedBeforeResponse};
}

async function reactPhotoWithFixture(t,fixture,extra=[]) {
  const profile=await profileFixture(t);
  return run(['react-photo','--profile',profile,'--photo-id',PHOTO_ID,...extra],{
    loadPlaywright:browserModule(()=>fixture.context),
  });
}

test('photo reaction only sends LIKE to feedback belonging to the requested Photo', async t=>{
  const fixture=photoReactionContext();
  const result=await reactPhotoWithFixture(t,fixture);
  assert.deepEqual(result,{state:'complete',reaction:'liked',photoId:'700'});
  assert.equal(fixture.accepted(),1);
  assert.equal(fixture.aborted(),0);
  assert.equal(fixture.closed(),1);
  assert.doesNotMatch(JSON.stringify(result),/ZmVlZGJhY2s/);
});

test('an already-liked Photo makes no mutation or click', async t=>{
  const fixture=photoReactionContext({labels:[POST_REMOVE]});
  const result=await reactPhotoWithFixture(t,fixture);
  assert.deepEqual(result,{state:'complete',reaction:'already_liked',photoId:'700'});
  assert.equal(fixture.clicks(),0);
  assert.equal(fixture.accepted(),0);
});

test('photo reaction waits for network completion and rejects failed mutation responses', async t=>{
  const delayed=photoReactionContext({responseDelay:20});
  assert.equal((await reactPhotoWithFixture(t,delayed)).reaction,'liked');
  assert.equal(delayed.closedBeforeResponse(),false);
  const failed=photoReactionContext({responseOk:false});
  assertBlocked(await reactPhotoWithFixture(t,failed),'browser_unavailable');
});

test('an HTTP 200 mutation with only optimistic remove state is not recorded as a liked Photo', async t=>{
  const fixture=photoReactionContext({afterClick:[POST_REMOVE],persistedLabels:[POST_LIKE]});
  assertBlocked(await reactPhotoWithFixture(t,fixture),'browser_unavailable');
  assert.equal(fixture.accepted(),1);
});

test('a post page or a different Photo cannot receive the requested photo reaction', async t=>{
  for(const currentUrl of [POST_URL,'https://www.facebook.com/photo/?fbid=701']) {
    const fixture=photoReactionContext({currentUrl});
    assertBlocked(await reactPhotoWithFixture(t,fixture),'browser_unavailable');
    assert.equal(fixture.clicks(),0);
    assert.equal(fixture.accepted(),0);
  }
});

test('photo reaction requires unambiguous direct Photo feedback instead of parent feedback', async t=>{
  const cases=[{feedbackUrl:POST_URL},{feedbackUrl:'https://www.facebook.com/photo/?fbid=701'},
    {missingFeedback:true},{extraPhoto:{__typename:'Photo',id:'700',feedback:{id:'PARENT_FEEDBACK',url:PHOTO_URL}}}];
  for(const options of cases) {
    const fixture=photoReactionContext(options);
    assertBlocked(await reactPhotoWithFixture(t,fixture),'browser_unavailable');
    assert.equal(fixture.clicks(),0);
  }
});

test('photo reaction aborts a mutation targeting parent feedback or removing a reaction', async t=>{
  for(const options of [{mutationFeedback:'PARENT_FEEDBACK'},{mutationReaction:'0'}]) {
    const fixture=photoReactionContext(options);
    assertBlocked(await reactPhotoWithFixture(t,fixture),'browser_unavailable');
    assert.equal(fixture.aborted(),1);
    assert.equal(fixture.accepted(),0);
  }
});

test('photo reaction requires confirmed remove state and matching Photo after its mutation', async t=>{
  for(const options of [{afterClick:[POST_LIKE,COMMENT_REMOVE]},
    {afterClickUrl:'https://www.facebook.com/photo/?fbid=701'}]) {
    const fixture=photoReactionContext(options);
    assertBlocked(await reactPhotoWithFixture(t,fixture),'browser_unavailable');
    assert.equal(fixture.accepted(),1);
  }
});

test('photo reaction refuses comment controls, duplicate controls and an unrelated URL argument', async t=>{
  for(const labels of [[COMMENT_REMOVE],[POST_LIKE,POST_LIKE]]) {
    const fixture=photoReactionContext({labels});
    assertBlocked(await reactPhotoWithFixture(t,fixture),'browser_unavailable');
    assert.equal(fixture.clicks(),0);
  }
  const fixture=photoReactionContext();
  assertBlocked(await reactPhotoWithFixture(t,fixture,['--url',POST_URL]),'invalid_arguments');
  assert.equal(fixture.clicks(),0);
});

async function run(args, options = {}) {
  // Guard before execution: older code must never fall through to a real browser.
  assert.equal(typeof collector.runCollector, 'function', 'collector has a structured execution boundary');
  return collector.runCollector(args, { stderr:{write(){}}, ...options });
}

function assertBlocked(result, reason) {
  assert.equal(result.state, 'blocked');
  assert.equal(result.reason, reason);
  assert.deepEqual(result.posts, []);
  assert.equal(result.skippedVideos, 0);
  assert.doesNotMatch(JSON.stringify(result), /DO-NOT-LOG|access_token|cookie=|\/Users\/private|SingletonLock/);
}

function loginContext(goto, { initialPages=1 } = {}) {
  const context = new EventEmitter();
  const pages = [];
  let closeCalls = 0;
  const addPage = () => {
    const page = new EventEmitter();
    page.goto = () => goto(context, page);
    page.close = async () => {
      const index = pages.indexOf(page);
      if (index < 0) return;
      pages.splice(index, 1);
      page.emit('close');
    };
    pages.push(page);
    context.emit('page', page);
    return page;
  };
  for (let index=0; index<initialPages; index++) addPage();
  context.pages = () => [...pages];
  context.newPage = async () => addPage();
  context.close = async () => { closeCalls++; context.emit('close'); };
  return { context, addPage, closeCalls:() => closeCalls };
}

async function within(promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error('login did not settle after the final page closed')), 400);
    })]);
  } finally { clearTimeout(timer); }
}

async function promptedLogin(profile, fixture) {
  let notifyPrompt;
  const prompted = new Promise(resolve => { notifyPrompt = resolve; });
  const result = run(['login','--profile',profile], {
    loadPlaywright:browserModule(() => fixture.context),
    stderr:{ write() { notifyPrompt(); } },
  });
  await within(prompted);
  return { result };
}

test('missing Playwright is distinct from a missing browser executable', async t => {
  const profile = await profileFixture(t);
  const dependency = await run(['login','--profile',profile], { loadPlaywright:async () => {
    throw Object.assign(new Error('DO-NOT-LOG /Users/private'), { code:'ERR_MODULE_NOT_FOUND' });
  }});
  assertBlocked(dependency, 'browser_dependency_missing');
  const browser = await run(['login','--profile',profile], { loadPlaywright:browserModule(() => {
    assert.fail('missing executable must not launch');
  }, join(profile, 'absent-browser')) });
  assertBlocked(browser, 'browser_missing');
});

test('missing and unsafe profiles are reported before importing Playwright', async t => {
  const root = await profileFixture(t);
  const file = join(root, 'not-a-directory');
  await writeFile(file, 'fixture');
  const link = join(root, 'profile-link');
  await symlink(root, link);
  for (const [profile, reason] of [[join(root,'absent'),'profile_missing'], [file,'profile_unavailable'], [link,'profile_unavailable']]) {
    const result = await run(['discover','--profile',profile], { loadPlaywright:() => assert.fail('unsafe profile must not load Playwright') });
    assertBlocked(result, reason);
  }
});

test('a profile in use is distinguished from other browser launch failures', async t => {
  const profile = await profileFixture(t);
  for (const [label, message, reason] of [
    ['profile in use', 'Failed to create a ProcessSingleton for your profile directory. This usually means that the profile is already in use by another instance of Chromium. /Users/private/SingletonLock cookie=DO-NOT-LOG', 'profile_in_use'],
    ['lock permission denied', 'SingletonLock: Permission denied /Users/private cookie=DO-NOT-LOG', 'browser_launch_failed'],
    ['permission denied with in-use summary', 'Permission denied. This usually means that the profile is already in use by another instance of Chromium. cookie=DO-NOT-LOG', 'browser_launch_failed'],
    ['browser crash', 'browser crashed access_token=DO-NOT-LOG /Users/private', 'browser_launch_failed'],
    ['missing selected executable', "Executable doesn't exist at /Users/private/DO-NOT-LOG", 'browser_missing'],
    ['browser download required', 'Please download new browsers. /Users/private/DO-NOT-LOG', 'browser_missing'],
  ]) {
    await t.test(label, async () => {
      const result = await run(['login','--profile',profile], { loadPlaywright:browserModule((path, options) => {
        assert.equal(path, profile);
        assert.equal(options.headless, false);
        throw new Error(message);
      }) });
      assertBlocked(result, reason);
    });
  }
});

test('failed Facebook navigation closes the login context and keeps its safe cause', async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext(async () => { throw new Error('net::ERR_FAILED access_token=DO-NOT-LOG'); });
  fixture.context.close = async () => { fixture.context.emit('close'); throw new Error('cleanup DO-NOT-LOG'); };
  let closed = false;
  fixture.context.on('close', () => { closed = true; });
  const result = await run(['login','--profile',profile], { loadPlaywright:browserModule(() => fixture.context) });
  assertBlocked(result, 'facebook_navigation_failed');
  assert.equal(closed, true);
});

test('closing login during navigation returns cancelled without hanging', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  for (const rejectNavigation of [false,true]) {
    const fixture = loginContext(async context => {
      context.emit('close');
      if (rejectNavigation) throw new Error('Target closed DO-NOT-LOG');
    });
    const result = await run(['login','--profile',profile], { loadPlaywright:browserModule(() => fixture.context) });
    assertBlocked(result, 'login_cancelled');
    assert.equal(fixture.closeCalls(), 1);
  }
});

test('closing login cancels navigation or page creation that stays pending', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  for (const creatingPage of [false,true]) {
    await t.test(creatingPage?'page creation':'navigation', {timeout:400}, async () => {
      const fixture = loginContext(context => { context.emit('close'); return new Promise(() => {}); });
      if (creatingPage) {
        fixture.context.pages = () => [];
        fixture.context.newPage = () => { fixture.context.emit('close'); return new Promise(() => {}); };
      }
      const result = await run(['login','--profile',profile], { loadPlaywright:browserModule(() => fixture.context) });
      assertBlocked(result, 'login_cancelled');
      assert.equal(fixture.closeCalls(), 1);
    });
  }
});

test('login completes when the user closes the context after the prompt', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext(async () => {});
  const result = await run(['login','--profile',profile], {
    loadPlaywright:browserModule(() => fixture.context),
    stderr:{ write(message) { assert.match(message, /自行登入/); fixture.context.emit('close'); } },
  });
  assert.equal(result.state, 'complete');
  assert.equal(fixture.closeCalls(), 1);
});

test('closing the final login page after the prompt completes without a native context close', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext(async () => {});
  const { result } = await promptedLogin(profile, fixture);
  await fixture.context.pages()[0].close();
  assert.equal((await within(result)).state, 'complete');
  assert.equal(fixture.closeCalls(), 1);
});

test('failed context shutdown after the final page closes returns a sanitized failure', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext(async () => {});
  let closeCalls = 0;
  fixture.context.close = async () => {
    closeCalls++;
    throw new Error('shutdown DO-NOT-LOG access_token=private');
  };
  const { result } = await promptedLogin(profile, fixture);
  await fixture.context.pages()[0].close();
  assertBlocked(await within(result), 'browser_unavailable');
  assert.equal(closeCalls, 1);
});

test('failed final-page shutdown during pending navigation keeps its safe shutdown cause', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext((context, page) => {
    void page.close();
    return new Promise(() => {});
  });
  fixture.context.close = async () => { throw new Error('shutdown DO-NOT-LOG'); };
  assertBlocked(await within(run(['login','--profile',profile], {
    loadPlaywright:browserModule(() => fixture.context),
  })), 'browser_unavailable');
});

test('closing the final login page cancels navigation that stays pending', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext((context, page) => {
    void page.close();
    return new Promise(() => {});
  });
  const result = await within(run(['login','--profile',profile], {
    loadPlaywright:browserModule(() => fixture.context),
  }));
  assertBlocked(result, 'login_cancelled');
  assert.equal(fixture.closeCalls(), 1);
});

test('final-page closure stays cancelled when navigation rejects before context shutdown finishes', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext(async (context, page) => {
    void page.close();
    throw new Error('Target closed DO-NOT-LOG');
  });
  let closeCalls = 0;
  fixture.context.close = async () => {
    closeCalls++;
    await new Promise(resolve => setImmediate(resolve));
    fixture.context.emit('close');
  };
  const result = await within(run(['login','--profile',profile], {
    loadPlaywright:browserModule(() => fixture.context),
  }));
  assertBlocked(result, 'login_cancelled');
  assert.equal(closeCalls, 1);
});

test('login keeps other existing pages open until the final page closes', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext(async () => {}, { initialPages:2 });
  const [first, last] = fixture.context.pages();
  const { result } = await promptedLogin(profile, fixture);
  await first.close();
  assert.equal(fixture.closeCalls(), 0);
  await last.close();
  assert.equal((await within(result)).state, 'complete');
  assert.equal(fixture.closeCalls(), 1);
});

test('login waits for a newly opened popup after the original page closes', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext(async () => {});
  const original = fixture.context.pages()[0];
  const { result } = await promptedLogin(profile, fixture);
  const popup = fixture.addPage();
  await original.close();
  assert.equal(fixture.closeCalls(), 0);
  await popup.close();
  assert.equal((await within(result)).state, 'complete');
  assert.equal(fixture.closeCalls(), 1);
});

test('an initially empty login context creates a page before waiting for its closure', { timeout:1000 }, async t => {
  const profile = await profileFixture(t);
  const fixture = loginContext(async () => {}, { initialPages:0 });
  const { result } = await promptedLogin(profile, fixture);
  assert.equal(fixture.closeCalls(), 0);
  assert.equal(fixture.context.pages().length, 1);
  await fixture.context.pages()[0].close();
  assert.equal((await within(result)).state, 'complete');
  assert.equal(fixture.closeCalls(), 1);
});

test('unknown execution failures never reuse caller-controlled reason or message', async t => {
  const profile = await profileFixture(t);
  const result = await run(['login','--profile',profile], { loadPlaywright:async () => {
    throw Object.assign(new Error('DO-NOT-LOG /Users/private cookie=private'), { reason:'profile_in_use' });
  }});
  assertBlocked(result, 'browser_unavailable');
});

test('CLI emits one sanitized blocked JSON result for invalid arguments', () => {
  const result = spawnSync(process.execPath, ['scripts/browser-collector.mjs','login','--profile','/Users/private/DO-NOT-LOG','--unexpected','access_token=DO-NOT-LOG'], {
    cwd:new URL('..', import.meta.url), encoding:'utf8', timeout:5000,
  });
  assert.equal(result.status, 2);
  assert.equal(result.stderr, '');
  assertBlocked(JSON.parse(result.stdout), 'invalid_arguments');
  assert.equal(result.stdout.trim().split('\n').length, 1);
});
