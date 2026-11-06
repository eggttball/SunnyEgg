#!/usr/bin/env node
// Dedicated-profile browser discovery. Never exports authentication or raw page data.
import { access, mkdir, lstat, rename, unlink, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const PAGE = 'https://www.facebook.com/profile.php?id=100063963161862';
const MAX_POST_PHOTOS = 200;
const SECRET = /^(?:access_token|appsecret_proof|oauth_token|token|authorization)$/i;
const PROGRESS_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const MESSAGES = {
  browser_dependency_missing:'Playwright 尚未安裝；請在專案目錄執行 npm install。',
  browser_missing:'Playwright Chromium 尚未安裝或無法執行；請執行 npx playwright install chromium。',
  profile_missing:'專用瀏覽器設定檔尚未建立；請先執行本機瀏覽器登入。',
  profile_unavailable:'專用瀏覽器設定檔無法安全使用；請確認目錄與讀寫權限。',
  profile_in_use:'專用瀏覽器設定檔正在使用；請先關閉使用該設定檔的瀏覽器，再重試。',
  browser_launch_failed:'專用瀏覽器無法啟動；請確認本機 Chromium 可執行後重試。',
  facebook_navigation_failed:'Facebook 頁面無法載入；請確認網路連線後重試。',
  login_cancelled:'登入頁面載入完成前瀏覽器已關閉；請重新執行登入。',
  invalid_arguments:'瀏覽器指令或參數無效；請確認指令、設定檔與網址。',
  browser_unavailable:'專用瀏覽器操作尚未完成；請確認本機瀏覽器設定後重試。',
  login_required:'專用瀏覽器登入已失效，請重新登入 Facebook。',
  post_unavailable:'指定貼文無法載入，或頁面尚未登入可查看。',
  post_no_photos:'指定貼文沒有可辨識的照片。',
  source_page_unverified:'無法確認指定貼文屬於小鹿托嬰中心；未下載照片。',
  album_incomplete:'Facebook 相簿無法完整瀏覽；未匯入不完整照片。',
  album_limit:'相簿照片超過單次處理上限；未匯入不完整照片。',
};

class CollectorError extends Error {
  constructor(reason) {super(MESSAGES[reason]);this.reason=reason;}
}

export function normalizePhotoLink(value) {
  try {
    if (typeof value !== 'string' || /[\\\s]/.test(value)) return null;
    const url = new URL(value);
    if (url.protocol !== 'https:' || !['facebook.com','www.facebook.com','m.facebook.com'].includes(url.hostname) || url.username || url.password || url.port) return null;
    if ([...url.searchParams.keys()].some(key => SECRET.test(key))) return null;
    if (!['/photo/','/photo','/photo.php'].includes(url.pathname)) return null;
    const photoId = url.searchParams.get('fbid');
    const match = /^pcb\.(\d+)$/.exec(url.searchParams.get('set') || '');
    if (!/^\d+$/.test(photoId || '')) return null;
    return match ? {photoId, albumPostId:match[1], sourceUrl:`https://www.facebook.com/photo/?fbid=${photoId}&set=pcb.${match[1]}`} :
      {photoId,sourceUrl:`https://www.facebook.com/photo/?fbid=${photoId}`};
  } catch { return null; }
}

export function normalizePostLink(value) {
  try {
    if (typeof value !== 'string' || /[\\\s]/.test(value)) return null;
    const url = new URL(value);
    if (url.protocol !== 'https:' || !['facebook.com','www.facebook.com','m.facebook.com'].includes(url.hostname) ||
        url.username || url.password || url.port || url.hash || [...url.searchParams.keys()].some(key => SECRET.test(key))) return null;
    if (url.pathname === '/permalink.php' || url.pathname === '/story.php') {
      const storyIds = url.searchParams.getAll('story_fbid'), pageIds = url.searchParams.getAll('id');
      if (storyIds.length !== 1 || pageIds.length !== 1 || !/^[A-Za-z0-9_.:-]{1,200}$/.test(storyIds[0]) || !/^\d+$/.test(pageIds[0])) return null;
      return {url:`https://www.facebook.com${url.pathname}?${new URLSearchParams({story_fbid:storyIds[0],id:pageIds[0]})}`,
        path:url.pathname, storyId:storyIds[0], pageId:pageIds[0]};
    }
    if (/^\/(?:[^/]+\/)?posts\/[A-Za-z0-9_.:-]+\/?$/.test(url.pathname)) {
      const path=url.pathname.replace(/\/$/,'');
      return {url:`https://www.facebook.com${path}`,path};
    }
    if (/^\/share\/p\/[A-Za-z0-9_.:-]+\/?$/.test(url.pathname)) {
      const path=url.pathname.replace(/\/$/,'');
      return {url:`https://www.facebook.com${path}`,path,share:true};
    }
    return null;
  } catch { return null; }
}

export function collectObservations(observations) {
  const posts = [], seen = new Set(), videoUrls = new Set();
  for (const item of observations) {
    if (!item || item.featured || item.pinned || item.inPost===false) continue;
    if (item.video || /facebook\.com\/(?:reel\/|watch\/?\?|(?:[^/]+\/)?videos\/)/.test(item.href || '')) {
      videoUrls.add(item.href); continue;
    }
    const photo = normalizePhotoLink(item.href);
    const identity=photo && (photo.albumPostId || `photo:${photo.photoId}`);
    if (!photo || seen.has(identity)) continue;
    seen.add(identity);
    posts.push({kind:'photos', ...photo});
  }
  return {posts, skippedVideos:videoUrls.size};
}

export function classifyPage({authenticated, loginForm, postSection, unavailable}) {
  if (loginForm || !authenticated) return 'login_required';
  if (unavailable) return 'page_unavailable';
  return postSection ? null : 'feed_unavailable';
}

async function snapshot(page, scroll=false) {
  return page.evaluate(shouldScroll => {
    const headings = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')];
    const postHeading = headings.find(node => /^(貼文|Posts)$/.test(node.textContent.trim()));
    const postSection = !!postHeading;
    const text = document.body.innerText.slice(0,6000);
    const postLinks = [];
    const observations = [...document.querySelectorAll('a[href]')].flatMap(link => {
      const href = link.href;
      if (!/facebook\.com\/(?:photo|reel\/|watch|(?:[^/]+\/)?videos\/)/.test(href)) return [];
      const article = link.closest('[role="article"], [data-pagelet^="FeedUnit"]');
      let card = article;
      // Facebook sometimes omits article roles; inspect a small local container only.
      if (!card) {
        card = link.parentElement;
        for (let i=0;i<3 && card?.parentElement;i++) card = card.parentElement;
      }
      const labels = [...(card?.querySelectorAll('[aria-label],h2,h3,[role="heading"]') || [])].map(node => node.getAttribute('aria-label') || node.textContent.trim());
      const timeLink=!![...(card?.querySelectorAll('a[href]') || [])].find(node=> /^(\d+\s*(分鐘|小時|天|週|minutes?|hours?|days?|weeks?)|\d{4}年|\d+月\d+日)/i.test(node.textContent.trim()));
      const inPost=!!article || /[?&]set=pcb\./.test(href) || timeLink;
      const pinned=labels.some(label => /^(置頂貼文|已置頂|Pinned post|Pinned)$/i.test(label));
      const featured=labels.some(label => /^(精選|Featured)$/i.test(label));
      if (inPost && !pinned && !featured) postLinks.push(link);
      return [{href, inPost, pinned, featured,
        video:!!card?.querySelector('video')}];
    });
    const root = document.scrollingElement || document.documentElement;
    const feed = document.querySelector('[role="feed"]');
    let scrollElement = root;
    // A feed can live in a nested scrolling panel. Choose its common scrollable
    // ancestor, rather than a photo carousel or a panel containing one card.
    let ancestor = feed && (!postLinks.length || postLinks.some(link => feed.contains(link))) ? feed : postLinks[0] || postHeading;
    for (;ancestor && ancestor!==root;ancestor=ancestor.parentElement) {
      if (/^(auto|scroll)$/.test(getComputedStyle(ancestor).overflowY) && ancestor.clientHeight>0 &&
          postLinks.every(link => ancestor.contains(link))) {
        scrollElement=ancestor;
        break;
      }
    }
    const viewport = scrollElement===root ? root.clientHeight || window.innerHeight : scrollElement.clientHeight;
    if (shouldScroll) {
      const step=Math.max(1,viewport*0.9);
      if (scrollElement===root) window.scrollBy(0,step);
      else scrollElement.scrollBy(0,step);
    }
    const top=scrollElement.scrollTop, height=scrollElement.scrollHeight;
    return {postSection, authenticated:!!document.querySelector('[aria-label^="帳號"],[aria-label^="Account"],[aria-label*="你的個人檔案"],[aria-label*="Your profile"]'),
      loginForm:!!document.querySelector('input[name="email"],input[name="pass"]'),
      unavailable:/內容暫時無法顯示|此內容目前無法查看|This content isn't available/.test(text),
      observations, scroll:{top,viewport,height,bottomDistance:Math.max(0,height-viewport-top),container:scrollElement===root?'document':'element'}};
  },scroll);
}

async function writeDiscoveryProgress(progressId, progress) {
  if (!progressId) return;
  const root = resolve(process.cwd());
  const dataDirectory = resolve(root, 'data');
  const runsDirectory = resolve(dataDirectory, 'runs');
  const target = resolve(runsDirectory, `.manual-progress-${progressId}.json`);
  const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
  try {
    const dataInfo = await lstat(dataDirectory);
    const runsInfo = await lstat(runsDirectory);
    if (dataInfo.isSymbolicLink() || runsInfo.isSymbolicLink() || !dataInfo.isDirectory() || !runsInfo.isDirectory()) return;
    await writeFile(temporary, JSON.stringify(progress) + '\n', { flag: 'wx', mode: 0o600 });
    await rename(temporary, target);
  } catch {
    await unlink(temporary).catch(() => {});
  }
}

async function contextFor(profile, login, loadPlaywright, extraOptions={}) {
  const path = resolve(profile);
  if (login) {
    try {await mkdir(path,{recursive:true,mode:0o700});}
    catch {throw new CollectorError('profile_unavailable');}
  }
  let info;
  try {info=await lstat(path);}
  catch (error) {throw new CollectorError(error.code==='ENOENT'?'profile_missing':'profile_unavailable');}
  if (info.isSymbolicLink() || !info.isDirectory()) throw new CollectorError('profile_unavailable');
  try {await access(path,constants.R_OK|constants.W_OK);}
  catch {throw new CollectorError('profile_unavailable');}

  let chromium;
  try {({chromium}=await loadPlaywright());}
  catch (error) {
    if (['ERR_MODULE_NOT_FOUND','MODULE_NOT_FOUND'].includes(error.code)) throw new CollectorError('browser_dependency_missing');
    throw error;
  }
  try {await access(chromium.executablePath(),constants.X_OK);}
  catch {throw new CollectorError('browser_missing');}
  try {
    return await chromium.launchPersistentContext(path,{...extraOptions,headless:!login,viewport:{width:1280,height:1000},locale:'zh-TW',timezoneId:'Asia/Taipei'});
  } catch (error) {
    // Recognize known local launch diagnostics only; never copy them into output.
    const message=typeof error?.message==='string'?error.message:'';
    if (/Permission denied|\bEACCES\b|\bEPERM\b|No space left on device|Read-only file system/i.test(message)) throw new CollectorError('browser_launch_failed');
    if (/profile(?: directory)? is (?:already )?in use|user data directory is (?:already )?in use|Opening in existing browser session|SingletonLock[^\n]*(?:File exists|already exists)/i.test(message)) throw new CollectorError('profile_in_use');
    // Headless mode can select a separate executable from executablePath().
    if (error?.code==='ENOENT' || /Executable doesn't exist|download new browsers/i.test(message)) throw new CollectorError('browser_missing');
    throw new CollectorError('browser_launch_failed');
  }
}

async function navigate(page, url) {
  try {await page.goto(url,{waitUntil:'domcontentloaded',timeout:60000});}
  catch {throw new CollectorError('facebook_navigation_failed');}
}

async function discover(profile, maxScrolls, loadPlaywright, progressId=null) {
  // Includes navigation and heading hydration; stays below the caller's 300s timeout.
  const deadline = Date.now()+240000;
  const context = await contextFor(profile,false,loadPlaywright);
  const capturedAt = new Date().toISOString();
  try {
    const page = context.pages()[0] || await context.newPage();
    await navigate(page,PAGE);
    await page.getByRole('heading',{name:/^(貼文|Posts)$/}).first().waitFor({timeout:20000}).catch(()=>{});
    const observations = new Map(), seenPosts = new Set();
    let scrolls=0, snapshots=0, loadWaitMs=0, reason='scan_limit';
    const record = state => {
      snapshots++;
      const error = classifyPage(state);
      if (error) return error;
      for (const item of state.observations) observations.set(JSON.stringify(item),item);
      for (const post of collectObservations(state.observations).posts) seenPosts.add(post.albumPostId || `photo:${post.photoId}`);
      return null;
    };
    const reportProgress = () => writeDiscoveryProgress(progressId, {
      phase:'scanning', scrolls, scrollLimit:maxScrolls, discoveredPosts:seenPosts.size,
    });
    let state=await snapshot(page), blocked=record(state);
    await reportProgress();
    while (!blocked && scrolls<maxScrolls && Date.now()<deadline) {
      state=await snapshot(page,true);
      scrolls++;
      blocked=record(state);
      await reportProgress();
      if (blocked) break;
      await page.waitForTimeout(Math.min(800,Math.max(0,deadline-Date.now())));
      state=await snapshot(page);
      blocked=record(state);
      await reportProgress();
      if (blocked) break;
      if (state.scroll.bottomDistance>2) continue;

      // Reaching the bottom only starts an idle check. Delayed loads and
      // constant-height virtualization can make new IDs appear without growth.
      const before=state.scroll, beforeCount=seenPosts.size;
      let waited=0, progress=false;
      while (waited<3000 && Date.now()<deadline) {
        const interval=Math.min(500,3000-waited,Math.max(0,deadline-Date.now()));
        await page.waitForTimeout(interval);
        waited+=interval;
        loadWaitMs+=interval;
        state=await snapshot(page);
        blocked=record(state);
        await reportProgress();
        if (blocked) break;
        const after=state.scroll;
        if (seenPosts.size>beforeCount || after.top!==before.top || after.height!==before.height ||
            after.viewport!==before.viewport || after.container!==before.container) {progress=true;break;}
      }
      if (blocked) break;
      if (!progress && waited>=3000) {reason='feed_stalled';break;}
    }
    if (blocked) return {version:1,state:'blocked',capturedAt,posts:[],skippedVideos:0,reason:blocked,message:'Facebook 登入或貼文讀取需要處理；未視為沒有貼文。'};
    const collected=collectObservations(observations.values());
    const coverage={state:'partial',reason,scrolls,snapshots,atBottom:state.scroll.bottomDistance<=2,
      observedPhotoPosts:collected.posts.length,observedVideos:collected.skippedVideos,loadWaitMs,scroll:state.scroll,
      budget:reason==='scan_limit' ? Date.now()>=deadline ? 'time_limit' : 'scroll_limit' : null};
    return {version:1,state:'partial',capturedAt,...collected,reason,coverage,
      message:'已掃描可見照片貼文；掃描範圍有限，未宣稱粉專所有最新貼文都已收齊。'};
  } finally {await context.close();}
}

async function inspectPost(profile,value,loadPlaywright) {
  const target=normalizePostLink(value);
  if(!target) throw new CollectorError('invalid_arguments');
  const context=await contextFor(profile,false,loadPlaywright);
  const capturedAt=new Date().toISOString();
  try {
    const page=context.pages()[0] || await context.newPage();
    await navigate(page,target.url);
    await page.waitForFunction(expected=>{
      const main=document.querySelector('[role="main"]') || document.body;
      const links=[...main.querySelectorAll('a[href]')];
      let effective=expected;
      if(expected.share) {
        try {
          const resolved=new URL(location.href);
          const storyId=resolved.searchParams.get('story_fbid'),pageId=resolved.searchParams.get('id');
          effective=storyId && pageId ? {storyId,pageId} : {path:resolved.pathname.replace(/\/$/,'')};
        } catch {}
      }
      const sameTarget=href=>{
        try {
          const link=new URL(href,location.href);
          if(!['www.facebook.com','facebook.com','m.facebook.com'].includes(link.hostname)) return false;
          if(effective.storyId) return ['permalink.php','story.php'].includes(link.pathname.slice(1)) &&
            link.searchParams.get('story_fbid')===effective.storyId && link.searchParams.get('id')===effective.pageId ||
            /\/posts\/([^/]+)\/?$/.test(link.pathname) &&
            decodeURIComponent(link.pathname.match(/\/posts\/([^/]+)\/?$/)[1])===effective.storyId;
          return link.pathname.replace(/\/$/,'')===(effective.path || expected.path);
        } catch {return false;}
      };
      const hasPostLink=links.some(link=>sameTarget(link.href));
      const hasPhotoLink=links.some(link=>{
        try {
          const photo=new URL(link.href);
          return ['www.facebook.com','facebook.com','m.facebook.com'].includes(photo.hostname) &&
            ['/photo/','/photo','/photo.php'].includes(photo.pathname) &&
            photo.searchParams.getAll('fbid').length===1 && /^\d+$/.test(photo.searchParams.get('fbid') || '');
        } catch {return false;}
      });
      const path=location.pathname.replace(/\/$/,'');
      const directRoute=expected.storyId
        ? (['/permalink.php','/story.php'].includes(path) &&
           new URL(location.href).searchParams.get('story_fbid')===expected.storyId) ||
          decodeURIComponent(path.match(/\/posts\/([^/]+)$/)?.[1] || '')===expected.storyId
        : path===(effective.path || expected.path);
      const text=document.body.innerText.slice(0,6000);
      return !!document.querySelector('input[name="email"],input[name="pass"]') ||
        /內容暫時無法顯示|此內容目前無法查看|This content isn't available/.test(text) ||
        (hasPhotoLink && (hasPostLink || directRoute));
    },target,{timeout:12000}).catch(()=>{});
    const observed=await page.evaluate(expected=>{
      const text=document.body.innerText.slice(0,6000);
      const main=document.querySelector('[role="main"]') || document.body;
      const articles=[...main.querySelectorAll('[role="article"],[data-pagelet^="FeedUnit"]')];
      let effective=expected;
      if(expected.share) {
        try {
          const resolved=new URL(location.href);
          const storyId=resolved.searchParams.get('story_fbid'),pageId=resolved.searchParams.get('id');
          effective=storyId && pageId ? {storyId,pageId} : {path:resolved.pathname.replace(/\/$/,'')};
        } catch {}
      }
      const sameTarget=href=>{
        try {
          const link=new URL(href,location.href);
          if(link.hostname!=='www.facebook.com' && link.hostname!=='facebook.com' && link.hostname!=='m.facebook.com') return false;
          if(effective.storyId) return ['permalink.php','story.php'].includes(link.pathname.slice(1)) &&
            link.searchParams.get('story_fbid')===effective.storyId && link.searchParams.get('id')===effective.pageId ||
            /\/posts\/([^/]+)\/?$/.test(link.pathname) &&
            decodeURIComponent(link.pathname.match(/\/posts\/([^/]+)\/?$/)[1])===effective.storyId;
          return link.pathname.replace(/\/$/,'')===(effective.path || expected.path);
        } catch {return false;}
      };
      let currentIsTarget=false;
      try {
        const current=new URL(location.href);
        if(expected.share) {
          currentIsTarget=/\/posts\/[^/]+\/?$/.test(current.pathname) ||
            ['/permalink.php','/story.php'].includes(current.pathname);
        } else if(expected.storyId) {
          currentIsTarget=(['/permalink.php','/story.php'].includes(current.pathname) &&
            current.searchParams.get('story_fbid')===expected.storyId && current.searchParams.get('id')===expected.pageId) ||
            decodeURIComponent(current.pathname.match(/\/posts\/([^/]+)\/?$/)?.[1] || '')===expected.storyId;
        } else currentIsTarget=current.pathname.replace(/\/$/,'')===expected.path;
      } catch {}
      const photoLinks=container=>[...container.querySelectorAll('a[href]')].filter(link=>{
        try {
          const url=new URL(link.href);
          return ['www.facebook.com','facebook.com','m.facebook.com'].includes(url.hostname) &&
            ['/photo/','/photo','/photo.php'].includes(url.pathname) &&
            url.searchParams.getAll('fbid').length===1 && /^\d+$/.test(url.searchParams.get('fbid') || '');
        } catch {return false;}
      });
      const targetLink=[...main.querySelectorAll('a[href]')].find(link=>sameTarget(link.href));
      const matchingArticle=articles.find(node=>[...node.querySelectorAll('a[href]')].some(link=>sameTarget(link.href)) &&
        photoLinks(node).length>0);
      let directPageAlbum=null;
      let scope=null;
      if(currentIsTarget) {
        const pagePhotos=photoLinks(document.body),groups=new Map();
        for(const link of pagePhotos) {
          const url=new URL(link.href),photoId=url.searchParams.get('fbid'),set=url.searchParams.get('set') || '';
          const key=set ? `set:${set}` : `photo:${photoId}`;
          if(!groups.has(key)) groups.set(key,[]);
          groups.get(key).push(link);
        }
        const title=document.title || '';
        const separator=title.indexOf(' - ');
        const postTitle=separator>=0 ? title.slice(separator+3).replace(/\s*\|\s*Facebook.*$/,'') : '';
        const normalizeText=value=>value.toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu,'');
        const titleKey=normalizeText(postTitle).slice(0,12);
        const titleMatches=[];
        if(titleKey.length>=6) {
          for(const [key,links] of groups) {
            let container=links[0];
            while(container && container!==document.body && !links.every(link=>container.contains(link))) container=container.parentElement;
            let depth=0,matchedDepth=null;
            for(let node=container;node;node=node.parentElement,depth++) {
              if(normalizeText(node.innerText || '').includes(titleKey)) {matchedDepth=depth;break;}
              if(node===main || depth>=12) break;
            }
            if(matchedDepth!==null) titleMatches.push({key,links,container,depth:matchedDepth});
          }
        }
        if(titleMatches.length) {
          const nearest=Math.min(...titleMatches.map(match=>match.depth));
          const best=titleMatches.filter(match=>match.depth===nearest);
          if(best.length===1) directPageAlbum={...best[0],videoCount:best[0].container.querySelectorAll('video').length};
        } else if(groups.size===1) {
          const [key,links]=groups.entries().next().value;
          let container=links[0];
          while(container && container!==document.body && !links.every(link=>container.contains(link))) container=container.parentElement;
          directPageAlbum={key,links,container,depth:null,videoCount:container?.querySelectorAll('video').length || 0};
        }
      } else scope=matchingArticle;
      if(!currentIsTarget && !scope && targetLink) {
        // Single-post pages can render the post in ordinary divs rather than
        // an article/feed unit. Stay close to the permalink and stop at the
        // first ancestor that contains an FB photo link.
        for(let node=targetLink,depth=0;node && node!==main && depth<12;node=node.parentElement,depth++) {
          if(photoLinks(node).length) {scope=node;break;}
        }
      }
      const photos=[];
      for(const link of directPageAlbum?.links || (scope ? photoLinks(scope) : [])) {
        const url=new URL(link.href);
        const ids=url.searchParams.getAll('fbid');
        photos.push({photoId:ids[0],href:url.href,hasMorePhotos:/^\+\d+$/.test(link.innerText.trim())});
      }
      const unique=[...new Map(photos.map(photo=>[photo.photoId,photo])).values()];
      const postContainer=directPageAlbum?.container || scope || matchingArticle || targetLink?.parentElement || main;
      const linkedPageId=[...(postContainer?.querySelectorAll('a[href]') || [])].map(link=>{
        try {
          const url=new URL(link.href);
          return url.pathname==='/profile.php' && /^\d+$/.test(url.searchParams.get('id') || '') ? url.searchParams.get('id') : null;
        } catch {return null;}
      }).find(Boolean);
      const publishedAt=postContainer?.querySelector('time[datetime]')?.getAttribute('datetime') || null;
      return {loginForm:!!document.querySelector('input[name="email"],input[name="pass"]'),
        authenticated:!!document.querySelector('[aria-label^="帳號"],[aria-label^="Account"],[aria-label*="你的個人檔案"],[aria-label*="Your profile"]'),
        unavailable:/內容暫時無法顯示|此內容目前無法查看|This content isn't available/.test(text),
        articleFound:!!matchingArticle || !!targetLink || currentIsTarget,photos:unique,
        pageId:expected.pageId || linkedPageId || null,publishedAt,
        videoCount:directPageAlbum?.videoCount ?? scope?.querySelectorAll('video').length ?? 0};
    },target);
    if(observed.loginForm || !observed.authenticated) throw new CollectorError('login_required');
    if(observed.unavailable || !observed.articleFound) throw new CollectorError('post_unavailable');
    if(!observed.photos.length) throw new CollectorError('post_no_photos');
    return {version:1,state:'complete',capturedAt,postPageId:observed.pageId,publishedAt:observed.publishedAt,
      photoSeeds:observed.photos,posts:[{kind:'photos',photoId:observed.photos[0].photoId,
        sourceUrl:observed.photos[0].href}],skippedVideos:observed.videoCount};
  } finally {await context.close();}
}

async function collectPostPhotos(profile,value,loadPlaywright,progressId=null) {
  const target=normalizePostLink(value);
  if(!target) throw new CollectorError('invalid_arguments');
  const inspection=await inspectPost(profile,value,loadPlaywright);
  if(inspection.state!=='complete') throw new CollectorError(inspection.reason || 'post_unavailable');
  if(!inspection.postPageId) throw new CollectorError('source_page_unverified');
  const seeds=Array.isArray(inspection.photoSeeds) ? inspection.photoSeeds : inspection.posts;
  if(!Array.isArray(seeds) || !seeds.length || seeds.length>MAX_POST_PHOTOS) throw new CollectorError('album_incomplete');
  const groups=new Map();
  for(const seed of seeds) {
    try {
      const url=new URL(seed.href);
      const photoId=url.searchParams.get('fbid'),set=url.searchParams.get('set') || '';
      if(url.protocol!=='https:' || !['facebook.com','www.facebook.com','m.facebook.com'].includes(url.hostname) ||
          !['/photo/','/photo','/photo.php'].includes(url.pathname) || !/^\d+$/.test(photoId || '')) throw new Error();
      const key=set ? `set:${set}` : `photo:${photoId}`;
      if(!groups.has(key)) groups.set(key,{href:url.href,set,seedPhotos:new Map(),hasMorePhotos:false});
      const group=groups.get(key);
      group.seedPhotos.set(photoId,url.href);
      group.hasMorePhotos ||= seed.hasMorePhotos===true;
    } catch {throw new CollectorError('album_incomplete');}
  }
  const context=await contextFor(profile,false,loadPlaywright);
  const photos=new Map();
  try {
    const page=context.pages()[0] || await context.newPage();
    await writeDiscoveryProgress(progressId,{phase:'collecting',currentPhoto:0});
    for(const group of groups.values()) {
      const readPhoto=()=>page.evaluate(()=>{
        const photoId=new URL(location.href).searchParams.get('fbid');
        const images=[...document.querySelectorAll('img')].filter(image=>{
          try {
            const url=new URL(image.currentSrc || image.src);
            return image.complete && image.naturalWidth>=300 && image.naturalHeight>=250 &&
              (url.hostname.endsWith('.fbcdn.net') || url.hostname.endsWith('.fbsbx.com'));
          } catch {return false;}
        }).sort((left,right)=>right.naturalWidth*right.naturalHeight-left.naturalWidth*left.naturalHeight);
        const image=images[0];
        return {photoId,sourceUrl:image ? image.currentSrc || image.src : null,
          width:image?.naturalWidth || 0,height:image?.naturalHeight || 0,
          set:new URL(location.href).searchParams.get('set') || ''};
      });
      const groupPhotos=new Map();
      try {
        await navigate(page,group.href);
        await page.waitForFunction(()=>{
          const id=new URL(location.href).searchParams.get('fbid');
          return /^\d+$/.test(id || '') && [...document.querySelectorAll('img')].some(image=>{
            try {
              const url=new URL(image.currentSrc || image.src);
              return image.complete && image.naturalWidth>=300 && image.naturalHeight>=250 &&
                (url.hostname.endsWith('.fbcdn.net') || url.hostname.endsWith('.fbsbx.com'));
            } catch {return false;}
          });
        },null,{timeout:15000}).catch(()=>{});
        let current=await readPhoto();
        if(!/^\d+$/.test(current.photoId || '') || !current.sourceUrl) throw new CollectorError('album_incomplete');
        const startId=current.photoId,localSeen=new Set();
        while(true) {
          if(localSeen.has(current.photoId)) {
            if(current.photoId===startId) break;
            throw new CollectorError('album_incomplete');
          }
          localSeen.add(current.photoId);
          const previous=groupPhotos.get(current.photoId);
          if(previous && previous.sourceUrl!==current.sourceUrl) groupPhotos.set(current.photoId,current);
          else if(!previous) groupPhotos.set(current.photoId,current);
          if(photos.size+groupPhotos.size>MAX_POST_PHOTOS) throw new CollectorError('album_limit');
          await writeDiscoveryProgress(progressId,{phase:'collecting',currentPhoto:photos.size+groupPhotos.size});

          const selectors=['button[aria-label="下一張相片"]','[role="button"][aria-label="下一張相片"]',
            'button[aria-label="Next photo"]','[role="button"][aria-label="Next photo"]'];
          let next=null;
          for(const selector of selectors) {
            const choices=page.locator(selector);
            for(let index=0;index<await choices.count();index++) {
              const candidate=choices.nth(index);
              if(await candidate.isVisible().catch(()=>false)) {next=candidate;break;}
            }
            if(next) break;
          }
          if(!next) {
            if(group.set.startsWith('a.') || group.set.startsWith('pcb.')) throw new CollectorError('album_incomplete');
            break;
          }
          const disabled=await next.getAttribute('aria-disabled').then(value=>value==='true').catch(()=>false) ||
            await next.isDisabled().catch(()=>false);
          if(disabled) break;
          if(photos.size+groupPhotos.size>=MAX_POST_PHOTOS) throw new CollectorError('album_limit');
          const before={photoId:current.photoId,sourceUrl:current.sourceUrl};
          try {await next.click({timeout:5000});}
          catch {throw new CollectorError('album_incomplete');}
          const transition=await page.waitForFunction(({photoId,sourceUrl})=>{
            const locationUrl=new URL(location.href);
            if(/\/videos?\//.test(locationUrl.pathname)) return 'non_photo';
            const currentId=locationUrl.searchParams.get('fbid');
            if(!currentId || currentId===photoId) return false;
            const hasNewImage=[...document.querySelectorAll('img')].some(image=>{
              try {
                const url=new URL(image.currentSrc || image.src);
                return image.complete && image.naturalWidth>=300 && image.naturalHeight>=250 &&
                  (url.hostname.endsWith('.fbcdn.net') || url.hostname.endsWith('.fbsbx.com')) &&
                  (image.currentSrc || image.src)!==sourceUrl;
              } catch {return false;}
            });
            return hasNewImage ? 'photo' : false;
          },before,{timeout:15000}).then(handle=>handle.jsonValue()).catch(()=>null);
          if(transition!=='photo') throw new CollectorError('album_incomplete');
          current=await readPhoto();
          if(current.photoId===startId) break;
          if(!/^\d+$/.test(current.photoId || '') || !current.sourceUrl) throw new CollectorError('album_incomplete');
        }
      } catch(error) {
        if(!(error instanceof CollectorError) || error.reason!=='album_incomplete' || group.hasMorePhotos) throw error;
        groupPhotos.clear();
        await writeDiscoveryProgress(progressId,{phase:'collecting',currentPhoto:photos.size});
        for(const [photoId,href] of group.seedPhotos) {
          await navigate(page,href);
          await page.waitForFunction(expectedId=>{
            const id=new URL(location.href).searchParams.get('fbid');
            return id===expectedId && [...document.querySelectorAll('img')].some(image=>{
              try {
                const url=new URL(image.currentSrc || image.src);
                return image.complete && image.naturalWidth>=300 && image.naturalHeight>=250 &&
                  (url.hostname.endsWith('.fbcdn.net') || url.hostname.endsWith('.fbsbx.com'));
              } catch {return false;}
            });
          },photoId,{timeout:15000}).catch(()=>{});
          const photo=await readPhoto();
          if(photo.photoId!==photoId || !photo.sourceUrl) throw new CollectorError('album_incomplete');
          groupPhotos.set(photoId,photo);
          await writeDiscoveryProgress(progressId,{phase:'collecting',currentPhoto:photos.size+groupPhotos.size});
        }
      }
      for(const [photoId,photo] of groupPhotos) {
        const previous=photos.get(photoId);
        if(!previous || previous.sourceUrl!==photo.sourceUrl) photos.set(photoId,photo);
        if(photos.size>MAX_POST_PHOTOS) throw new CollectorError('album_limit');
      }
    }
  } finally {await context.close();}
  if(!photos.size) throw new CollectorError('post_no_photos');
  return {version:1,state:'complete',capturedAt:new Date().toISOString(),postPageId:inspection.postPageId,
    publishedAt:inspection.publishedAt,skippedVideos:inspection.skippedVideos,
    photos:[...photos.values()].map(({photoId,sourceUrl})=>({photoId,sourceUrl}))};
}

async function react(profile,value,loadPlaywright) {
  let url;
  try {url=new URL(value);}
  catch {throw new CollectorError('invalid_arguments');}
  if (url.protocol!=='https:' || !['www.facebook.com','facebook.com'].includes(url.hostname) || url.username || url.password || url.port || [...url.searchParams.keys()].some(key=>SECRET.test(key))) throw new CollectorError('invalid_arguments');
  if (url.pathname!=='/permalink.php' && !/^\/(?:[^/]+\/)?posts\//.test(url.pathname)) throw new CollectorError('invalid_arguments');
  const context = await contextFor(profile,false,loadPlaywright);
  try {
    const page = context.pages()[0] || await context.newPage();
    await navigate(page,url.href);
    const remove = page.getByRole('button',{name:/^(?:從小鹿托嬰中心的貼文移除讚|對小鹿托嬰中心的貼文收回讚)$/});
    const like = page.getByRole('button',{name:/^對小鹿托嬰中心的貼文表示讚$/});
    // Hydration may reveal either state. Strict matching rejects ambiguous controls.
    await like.or(remove).waitFor({state:'visible',timeout:15000});
    const removeCount = await remove.count(), likeCount = await like.count();
    if (removeCount + likeCount !== 1) throw new CollectorError('browser_unavailable');
    if (removeCount) return {state:'complete',reaction:'already_liked'};
    await like.click();
    await remove.waitFor({state:'visible',timeout:10000});
    return {state:'complete',reaction:'liked'};
  } finally {await context.close();}
}

function requirePhotoPage(page,photoId) {
  const photo=normalizePhotoLink(page.url());
  if (!photo || photo.photoId!==photoId || new URL(page.url()).searchParams.getAll('fbid').length!==1)
    throw new CollectorError('browser_unavailable');
}

async function photoFeedback(page,photoId) {
  // The photo viewer still labels this button as a "post". Resolve feedback
  // through the requested Photo itself, never through its container story.
  return page.evaluate(expected=>{
    const targets=new Set();let invalid=false,budget=60000,totalBytes=0;
    for(const script of document.querySelectorAll('script[type="application/json"]')) {
      const text=script.textContent;
      totalBytes+=text.length;
      if(totalBytes>16*1024*1024) return null;
      let parsed;
      try {parsed=JSON.parse(text);} catch {continue;}
      const stack=[[parsed,0]];
      while(stack.length) {
        if(--budget<0) return null;
        const [value,depth]=stack.pop();
        if(!value || typeof value!=='object' || depth>50) continue;
        if(value.__typename==='Photo' && value.id===expected && value.feedback) {
          const feedback=value.feedback;
          try {
            const url=new URL(feedback.url);
            if(typeof feedback.id!=='string' || !/^[A-Za-z0-9+/=_:.\-]{1,512}$/.test(feedback.id) ||
                url.protocol!=='https:' || !['www.facebook.com','facebook.com','m.facebook.com'].includes(url.hostname) ||
                url.username || url.password || url.port || !['/photo/','/photo','/photo.php'].includes(url.pathname) ||
                url.searchParams.getAll('fbid').length!==1 || url.searchParams.get('fbid')!==expected) invalid=true;
            else targets.add(feedback.id);
          } catch {invalid=true;}
        }
        for(const child of Object.values(value)) if(child && typeof child==='object') stack.push([child,depth+1]);
      }
    }
    return !invalid && targets.size===1 ? [...targets][0] : null;
  },photoId);
}

async function reactPhoto(profile,photoId,loadPlaywright) {
  if(typeof photoId!=='string' || !/^[1-9]\d{0,31}$/.test(photoId)) throw new CollectorError('invalid_arguments');
  const context=await contextFor(profile,false,loadPlaywright,{serviceWorkers:'block'});
  try {
    const page=context.pages()[0] || await context.newPage();
    await navigate(page,`https://www.facebook.com/photo/?fbid=${photoId}`);
    requirePhotoPage(page,photoId);
    const remove=page.getByRole('button',{name:/^(?:從小鹿托嬰中心的貼文移除讚|對小鹿托嬰中心的貼文收回讚)$/});
    const like=page.getByRole('button',{name:/^對小鹿托嬰中心的貼文表示讚$/});
    await like.or(remove).waitFor({state:'visible',timeout:15000});
    requirePhotoPage(page,photoId);
    const feedbackId=await photoFeedback(page,photoId);
    if(!feedbackId || await remove.count()+await like.count()!==1) throw new CollectorError('browser_unavailable');
    if(await remove.count()) return {state:'complete',reaction:'already_liked',photoId};

    let accepted=0,rejected=false,confirmation=Promise.resolve(false);
    // Inspect mutation variables only in memory. Unknown targets and removal
    // reactions never leave this browser; no secret request data is logged.
    await page.route(/^https:\/\/(?:www\.|m\.)?facebook\.com\/api\/graphql\/?(?:\?.*)?$/,async route=>{
      const request=route.request();
      if(request.method()!=='POST') {await route.continue();return;}
      const form=new URLSearchParams(request.postData() || '');
      const name=form.get('fb_api_req_friendly_name') || '';
      if(name && !name.endsWith('Mutation')) {await route.continue();return;}
      let input;
      try {input=JSON.parse(form.get('variables') || '{}').input;} catch {}
      if(name==='CometUFIFeedbackReactMutation' && input?.feedback_id===feedbackId &&
          input.feedback_reaction_id==='1635855486666999' && accepted===0 && !rejected) {
        accepted++;
        confirmation=route.continue().then(async()=>{
          const response=await request.response();
          if(!response) return false;
          const failure=await response.finished();
          return response.ok() && !failure;
        }).catch(()=>false);
      } else {rejected=true;await route.abort();}
    });
    await like.click();
    await remove.waitFor({state:'visible',timeout:10000});
    let timer,confirmed;
    try {
      confirmed=await Promise.race([confirmation,new Promise(resolve=>{timer=setTimeout(()=>resolve(false),10000);})]);
    } finally {clearTimeout(timer);}
    requirePhotoPage(page,photoId);
    if(!confirmed || rejected || accepted!==1 || await remove.count()!==1 || await like.count()!==0)
      throw new CollectorError('browser_unavailable');
    // GraphQL can return HTTP 200 errors while the UI is optimistic. Reload
    // the canonical Photo from the server and confirm its persisted state.
    await navigate(page,`https://www.facebook.com/photo/?fbid=${photoId}`);
    await like.or(remove).waitFor({state:'visible',timeout:15000});
    requirePhotoPage(page,photoId);
    if(await photoFeedback(page,photoId)!==feedbackId || await remove.count()!==1 || await like.count()!==0)
      throw new CollectorError('browser_unavailable');
    return {state:'complete',reaction:'liked',photoId};
  } finally {await context.close();}
}

export async function main(args=process.argv.slice(2), {loadPlaywright=()=>import('playwright'),stderr=process.stderr}={}) {
  args=[...args];
  const command=args.shift();
  const options={};
  while(args.length) {const key=args.shift();if(!['--profile','--max-scrolls','--url','--photo-id','--progress-id'].includes(key) || !args.length || Object.hasOwn(options,key)) throw new CollectorError('invalid_arguments');options[key]=args.shift();}
  if(!['login','discover','inspect-post','collect-post-photos','react','react-photo'].includes(command) || !options['--profile'] ||
      (command!=='react-photo' && Object.hasOwn(options,'--photo-id')) ||
      (!['discover','inspect-post','collect-post-photos','react'].includes(command) && Object.hasOwn(options,'--url')) ||
      (!['discover','collect-post-photos'].includes(command) && Object.hasOwn(options,'--progress-id')) ||
      (Object.hasOwn(options,'--progress-id') && !PROGRESS_ID.test(options['--progress-id']))) throw new CollectorError('invalid_arguments');
  if(command==='react-photo') {
    if(Object.keys(options).some(key=>!['--profile','--photo-id'].includes(key))) throw new CollectorError('invalid_arguments');
    return reactPhoto(options['--profile'],options['--photo-id'],loadPlaywright);
  }
  if(command==='login') {
    const context=await contextFor(options['--profile'],true,loadPlaywright);
    let closed=false, loaded=false, finishClosure, failClosure;
    const closure=new Promise((resolve,reject)=>{
      finishClosure=()=>{closed=true;resolve();};
      failClosure=()=>reject(new CollectorError('browser_unavailable'));
    });
    context.once('close',finishClosure);
    let closing;
    const closeContext=()=>closing ||= context.close();
    const watchedPages=new Set();
    const closeIfEmpty=()=>{if(!closed && context.pages().length===0) void closeContext().then(finishClosure,failClosure);};
    const watchPage=page=>{
      if(watchedPages.has(page)) return;
      watchedPages.add(page);
      page.once('close',closeIfEmpty);
    };
    context.on('page',watchPage);
    for(const page of context.pages()) watchPage(page);
    try {
      const navigation=(async()=>{
        const page=context.pages()[0] || await context.newPage();
        await navigate(page,PAGE);
      })();
      await Promise.race([navigation,closure.then(()=>{throw new CollectorError('login_cancelled');})]);
      if(closed || closing) throw new CollectorError('login_cancelled');
      loaded=true;
      stderr.write('請在專用瀏覽器自行登入 Facebook；完成後退出整個專用瀏覽器（macOS 可按 ⌘Q）。此設定檔只供 SunnyEgg 使用。\n');
      await closure;
      return {state:'complete',message:'專用瀏覽器已關閉；下次執行會檢查登入是否有效。'};
    } catch(error) {
      if(error instanceof CollectorError && error.reason==='browser_unavailable') throw error;
      if(!loaded && (closed || closing)) throw new CollectorError('login_cancelled');
      throw error;
    } finally {
      context.off('close',finishClosure);
      context.off('page',watchPage);
      for(const page of watchedPages) page.off('close',closeIfEmpty);
      await closeContext().catch(()=>{});
    }
  }
  if(command==='discover') {
    const max=Number(options['--max-scrolls'] || 12);
    if(!Number.isInteger(max) || max<1 || max>100) throw new CollectorError('invalid_arguments');
    return discover(options['--profile'],max,loadPlaywright,options['--progress-id'] || null);
  }
  if(command==='inspect-post' && options['--url']) return inspectPost(options['--profile'],options['--url'],loadPlaywright);
  if(command==='collect-post-photos' && options['--url']) return collectPostPhotos(options['--profile'],options['--url'],loadPlaywright,options['--progress-id'] || null);
  if(command==='react' && options['--url']) return react(options['--profile'],options['--url'],loadPlaywright);
  throw new CollectorError('invalid_arguments');
}

export async function runCollector(args=process.argv.slice(2), options={}) {
  try {return await main(args,options);}
  catch (error) {
    const reason=error instanceof CollectorError && Object.hasOwn(MESSAGES,error.reason)?error.reason:'browser_unavailable';
    return {version:1,state:'blocked',capturedAt:new Date().toISOString(),posts:[],skippedVideos:0,reason,message:MESSAGES[reason]};
  }
}

if (process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
  const result=await runCollector();
  process.stdout.write(JSON.stringify(result)+'\n');
  process.exitCode=result.state==='blocked'?2:0;
}
