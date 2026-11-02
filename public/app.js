"use strict";

const ui = {
  content: document.querySelector("#gallery-content"),
  status: document.querySelector("#fetch-status"),
  jobStatus: document.querySelector("#job-status"),
  postAudits: document.querySelector("#post-audits"),
  schedule: document.querySelector("#schedule-summary"),
  error: document.querySelector("#api-error"),
  refresh: document.querySelector("#refresh"),
  date: document.querySelector("#date-filter"),
  monthNav: document.querySelector("#month-nav"),
  fetchForm: document.querySelector("#manual-fetch-form"),
  fetchControls: document.querySelector("#manual-fetch-controls"),
  fetchPreset: document.querySelector("#fetch-preset"),
  fetchStart: document.querySelector("#fetch-start-date"),
  fetchEnd: document.querySelector("#fetch-end-date"),
  fetchRange: document.querySelector("#fetch-range-summary"),
  fetchSubmit: document.querySelector("#manual-fetch-submit"),
  manualStatus: document.querySelector("#manual-fetch-status"),
  manualPostForm: document.querySelector("#manual-post-form"),
  manualPostUrl: document.querySelector("#manual-post-url"),
  manualPostSubmit: document.querySelector("#manual-post-submit"),
  manualPhotoForm: document.querySelector("#manual-photo-form"),
  manualPhotoUrl: document.querySelector("#manual-photo-url"),
  manualPhotoSubmit: document.querySelector("#manual-photo-submit"),
  manualPhotoStatus: document.querySelector("#manual-photo-status"),
};
let gallery = null;
let galleryRequestId = 0;
let filter = "candidate";
let manualOptions = null;
let manualOptionsFailed = false;
let manualBusy = true;
let manualRun = null;
let manualPollTimer = null;
let manualPhotoBusy = false;
let manualPhotoPollTimer = null;
const controlMode = new URLSearchParams(window.location.search).get("ctrl") === "1";
document.querySelectorAll("[data-control-only]").forEach(node => { node.hidden = !controlMode; });
const feedbackBusy = new Set();
const feedbackErrors = new Map();
let logicalGallery = null;
let logicalPhotoIndex = new Map();
const publishedDateFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
});
const runDateFormatter = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Asia/Taipei", year: "numeric", month: "numeric", day: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function sourceLink(url, label = "查看原貼文 ↗") {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || !(parsed.hostname === "facebook.com" || parsed.hostname.endsWith(".facebook.com")) || parsed.username || parsed.password) return null;
    const link = element("a", "source-link", label);
    link.href = parsed.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute("aria-label", `${label.replace("↗", "").trim()}（在新分頁開啟）`);
    return link;
  } catch { return null; }
}

function mediaUrl(path) {
  if (typeof path !== "string" || !path.startsWith("media/") || path.includes("\\")) return null;
  const parts = path.split("/");
  if (parts.some(part => !part || part === "." || part === "..")) return null;
  return `/${parts.map(encodeURIComponent).join("/")}`;
}

function dateKey(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}(?:T|$)/.test(value) || !Number.isFinite(Date.parse(value))) return "unknown";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const parts = publishedDateFormatter.formatToParts(new Date(value));
  const get = type => parts.find(part => part.type === type).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function dateLabel(value) {
  if (value === "unknown") return "日期未提供";
  const [year, month, day] = value.split("-");
  return `${year} 年 ${Number(month)} 月 ${Number(day)} 日`;
}

function newestFirst(posts) {
  const publishTime = post => {
    const value = post.publishedAt;
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}(?:T|$)/.test(value)) return -Infinity;
    const day = value.slice(0, 10);
    const dayStart = Date.parse(`${day}T00:00:00+08:00`);
    if (!Number.isFinite(dayStart) || dateKey(new Date(dayStart).toISOString()) !== day) return -Infinity;
    const time = value.length === 10 ? dayStart : Date.parse(value);
    return Number.isFinite(time) ? time : -Infinity;
  };
  return posts.map((post, index) => ({ post, index, time: publishTime(post) }))
    .sort((first, second) => first.time === second.time ? first.index - second.index : first.time > second.time ? -1 : 1)
    .map(entry => entry.post);
}

function sourcePhotoId(post, asset) {
  const current = asset?.sourcePhotoId;
  if (typeof current === "string" && /^\d+$/.test(current)) return current;
  const assets = Array.isArray(post?.assets) ? post.assets : [];
  const index = assets.findIndex(entry => entry?.id === asset?.id);
  const legacy = index >= 0 && Array.isArray(post?.dailyPhotoIds) && post.dailyPhotoIds.length === assets.length
    ? post.dailyPhotoIds[index] : null;
  return typeof legacy === "string" && /^\d+$/.test(legacy) ? legacy : null;
}

function indexLogicalPhotos() {
  if (logicalGallery === gallery) return;
  const records = [];
  const byId = new Map();
  const validId = value => typeof value === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(value);
  logicalPhotoIndex = new Map(gallery.posts.map(post => [post, []]));
  for (const post of gallery.posts) {
    const assets = Array.isArray(post.assets) ? post.assets : [];
    const results = Array.isArray(post.recognition?.results) ? post.recognition.results : [];
    for (const asset of assets) {
      if (!asset || asset.type !== "image") continue;
      const photoId = sourcePhotoId(post, asset);
      const record = { post, asset, index: records.length,
        photoId,
        result: results.find(result => result && result.assetId === asset.id) };
      records.push(record);
      if (validId(post.id) && validId(asset.id)) byId.set(asset.id, byId.has(asset.id) ? null : record);
    }
  }
  const parents = records.map(record => record.index);
  const identities = records.map(record => record.photoId);
  const root = index => {
    while (parents[index] !== index) { parents[index] = parents[parents[index]]; index = parents[index]; }
    return index;
  };
  const join = (a, b) => {
    const first = root(a), second = root(b);
    if (first === second) return true;
    if (identities[first] && identities[second] && identities[first] !== identities[second]) return false;
    parents[second] = first;
    identities[first] ||= identities[second];
    return true;
  };
  const byPhotoId = new Map();
  for (const record of records) {
    if (byId.get(record.asset.id) !== record || !record.photoId) continue;
    if (byPhotoId.has(record.photoId)) join(byPhotoId.get(record.photoId), record.index);
    else byPhotoId.set(record.photoId, record.index);
  }
  const aliases = new Set();
  const chains = new Map();
  for (const record of records) {
    if (byId.get(record.asset.id) !== record) continue;
    let current = record;
    const path = [], visited = new Set();
    while (current && !visited.has(current.index)) {
      visited.add(current.index);
      path.push(current);
      const target = current.asset.duplicateOf;
      if (target == null) {
        if (!chains.has(current)) chains.set(current, new Set());
        path.forEach(member => chains.get(current).add(member));
        break;
      }
      if (!validId(target)) break;
      current = byId.get(target);
    }
  }
  for (const [canonical, members] of chains) {
    const photoIds = new Set([...members].map(member => member.photoId).filter(Boolean));
    if (photoIds.size > 1) continue;
    for (const member of members) {
      join(canonical.index, member.index);
      if (member !== canonical) aliases.add(member.index);
    }
  }
  const groups = new Map();
  for (const record of records) {
    const identity = root(record.index);
    if (!groups.has(identity)) groups.set(identity, []);
    groups.get(identity).push(record);
  }
  for (const members of groups.values()) {
    const canonical = members.find(record => !aliases.has(record.index)) || members[0];
    const manualSelection = members.some(record => record.asset.manualSelection === true);
    logicalPhotoIndex.get(canonical.post).push({ result: manualSelection ? { state: "candidate" } : canonical.result,
      asset: { ...canonical.asset, manualSelection, userExcluded: members.some(record => record.asset.userExcluded === true),
        logicalMembers: members.map(record => record.asset) } });
  }
  logicalGallery = gallery;
}

function photos(post) {
  indexLogicalPhotos();
  return logicalPhotoIndex.get(post) || [];
}

function feedbackKey(postId, assetId) {
  return JSON.stringify([postId, assetId]);
}

async function submitPhotoFeedback(postId, asset, button) {
  const key = feedbackKey(postId, asset.id);
  if (!controlMode || feedbackBusy.has(key)) return;
  const excluded = asset.userExcluded !== true;
  feedbackBusy.add(key);
  feedbackErrors.delete(key);
  button.disabled = true;
  button.textContent = "儲存中…";
  try {
    const { response, data } = await manualRequest("/api/photo-feedback", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ postId, assetId: asset.id, excluded }),
    });
    if (!response.ok || data.postId !== postId || data.assetId !== asset.id || data.excluded !== excluded) {
      throw new Error("Feedback unavailable");
    }
    asset.userExcluded = excluded;
    for (const member of asset.logicalMembers || []) member.userExcluded = excluded;
    logicalGallery = null;
    render();
    await loadGallery();
  } catch {
    feedbackErrors.set(key, "未能儲存這張照片的標記，請重試。");
  } finally {
    feedbackBusy.delete(key);
    button.disabled = false;
    render();
  }
}

function mediaCard(postId, asset, result, photoIndex, photoId=null) {
  const card = element("figure", "media-card");
  if (asset.userExcluded === true) card.classList.toggle("user-excluded", true);
  const url = mediaUrl(asset.path);
  if (!url) {
    card.append(element("p", "media-error", "無法顯示這張照片：本機照片路徑不符合格式。"));
    return card;
  }
  const image = element("img");
  image.alt = "托嬰中心照片";
  image.loading = "lazy";
  image.decoding = "async";
  image.addEventListener("error", () => {
    if (image.hidden) return;
    image.hidden = true;
    const message = element("p", "media-error", "無法讀取本機照片。請確認原始檔案仍在資料夾內。" );
    message.setAttribute("role", "status");
    card.prepend(message);
  });
  image.src = url;
  const candidatePhotoId = result?.state === "candidate" && typeof photoId === "string" && /^\d+$/.test(photoId)
    ? photoId : null;
  if (candidatePhotoId) {
    const originalUrl = "https://www.facebook.com/photo/?fbid=" + encodeURIComponent(candidatePhotoId);
    const imageLink = element("a", "media-source-image-link");
    imageLink.href = originalUrl;
    imageLink.target = "_blank";
    imageLink.rel = "noopener noreferrer";
    imageLink.setAttribute("aria-label", "在新分頁開啟這張照片的 Facebook 原照");
    imageLink.title = "在 Facebook 查看原照片";
    imageLink.append(image);
    card.append(imageLink);
  } else card.append(image);
  const caption = element("figcaption", "media-caption");
  if (candidatePhotoId) {
    const originalLink = element("a", "photo-source-link", "查看原照片 ↗");
    originalLink.href = "https://www.facebook.com/photo/?fbid=" + encodeURIComponent(candidatePhotoId);
    originalLink.target = "_blank";
    originalLink.rel = "noopener noreferrer";
    originalLink.setAttribute("aria-label", "在新分頁開啟這張照片的 Facebook 原照");
    caption.append(originalLink);
  }
  const labels = {
    no_match: "人臉比對未命中 · 不代表孩子沒有出現",
    no_face: "未偵測到人臉 · 尚無可供比對的人臉",
    error: "這張照片辨識失敗 · 尚未完成檢查",
  };
  if (asset.manualSelection !== true && result?.state !== "candidate") {
    caption.append(element("span", "match-label", labels[result?.state] || "尚未產生辨識結果"));
  }
  if (controlMode) {
    const key = feedbackKey(postId, asset.id);
    if (asset.userExcluded === true) caption.append(element("span", "photo-excluded-label", "已排除 · 不會出現在一般相簿"));
    const button = element("button", "photo-feedback-button", feedbackBusy.has(key) ? "儲存中…"
      : asset.userExcluded === true ? "恢復照片" : "排除");
    button.type = "button";
    button.disabled = feedbackBusy.has(key);
    button.setAttribute("aria-label", `${asset.userExcluded === true ? "恢復照片" : "排除"}：第 ${photoIndex + 1} 張，${asset.name || "未命名照片"}`);
    button.addEventListener("click", () => submitPhotoFeedback(postId, asset, button));
    caption.append(button);
    if (feedbackErrors.has(key)) {
      const error = element("p", "photo-feedback-error", feedbackErrors.get(key));
      error.setAttribute("role", "status");
      caption.append(error);
    }
  }
  card.append(caption);
  return card;
}

function postCard(post, displayedPhotos, monthAnchorId=null, latest=false) {
  const article = element("article", "post-card");
  article.dataset.postId = post.id;
  if (monthAnchorId) article.id = monthAnchorId;
  else if (latest) article.id = "latest-post";
  if (monthAnchorId && latest) {
    const latestAnchor = element("span", "post-anchor");
    latestAnchor.id = "latest-post";
    latestAnchor.setAttribute("aria-hidden", "true");
    article.prepend(latestAnchor);
  }
  article.dataset.photoLayout = displayedPhotos.length >= 3 ? "three-plus" : String(displayedPhotos.length);
  const header = element("div", "post-header");
  const heading = element("div", "post-heading");
  heading.append(element("h3", "", post.title || "照片貼文"));
  const meta = element("div", "post-meta");
  meta.append(element("span", "", dateLabel(dateKey(post.publishedAt))));
  const link = sourceLink(post.sourceUrl);
  if (link) meta.append(link);
  heading.append(meta);
  header.append(heading);
  article.append(header);
  const grid = element("div", "media-grid");
  displayedPhotos.forEach(({ asset, result }, index) =>
    grid.append(mediaCard(post.id, asset, result, index, sourcePhotoId(post, asset))));
  article.append(grid);
  return article;
}

function setFilter(value) {
  filter = value === "all" ? "all" : "candidate";
  document.querySelectorAll("[data-filter]").forEach(button => {
    const active = button.dataset.filter === filter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function updateMonthNavigation(entries) {
  ui.monthNav.replaceChildren();
  const latestPost = entries[0]?.post || null;
  const addLink = (label, targetId) => {
    const link = element("a", "nav-item archive-nav-item", label);
    link.href = `#${targetId}`;
    link.addEventListener("click", event => {
      if (ui.date.value === "all") return;
      event.preventDefault();
      ui.date.value = "all";
      render();
      const target = document.getElementById(targetId);
      if (target) {
        const url = new URL(window.location.href);
        url.hash = targetId;
        window.history.replaceState(null, "", url);
        target.scrollIntoView({ block: "start", behavior: "auto" });
      }
    });
    ui.monthNav.append(link);
  };
  addLink("最新貼文", latestPost ? "latest-post" : "gallery");
  const firstPostByMonth = new Map();
  for (const { post } of entries) {
    const date = dateKey(post.publishedAt);
    if (date !== "unknown") {
      const month = date.slice(0, 7);
      if (!firstPostByMonth.has(month)) firstPostByMonth.set(month, post);
    }
  }
  for (const [month] of firstPostByMonth) {
    const [year, number] = month.split("-");
    addLink(`${year} 年 ${Number(number)} 月`, `month-${month}`);
  }
}

function emptyState(photoCount) {
  const section = element("div", "empty-state");
  const illustration = element("div", "empty-illustration");
  illustration.setAttribute("aria-hidden", "true");
  illustration.append(element("span", "photo-shape"), element("span", "photo-shape photo-front"), element("span", "sparkle", "✳"));
  section.append(illustration);
  if (filter === "candidate") {
    section.append(element("h3", "", ui.date.value === "all" ? "目前沒有候選照片" : "這個日期沒有候選照片"));
    section.append(element("p", "", photoCount ? "可以查看本機保留照片，確認既有照片的比對結果。新抓取的照片只有候選會保留；逐篇檢查紀錄會列出比對數量。" : "新抓取的照片只有候選會加入相簿；沒有保留照片不代表尚未檢查。請查看上方逐篇照片檢查紀錄。"));
    if (photoCount && controlMode) {
      const inspect = element("button", "button", "查看本機保留照片");
      inspect.type = "button";
      inspect.addEventListener("click", () => { setFilter("all"); ui.date.value = "all"; render(); });
      section.append(inspect);
    }
  } else {
    section.append(element("h3", "", photoCount ? "這個日期沒有照片" : "相簿，正等著第一個小日常"));
    section.append(element("p", "", photoCount ? "試試其他日期，看看已保留的照片。" : "目前尚未加入照片。每日照片流程完成後，可在這裡查看候選與已保留的照片。"));
    if (photoCount) {
      const reset = element("button", "button", "查看所有日期");
      reset.type = "button";
      reset.addEventListener("click", () => { ui.date.value = "all"; render(); });
      section.append(reset);
    }
  }
  return section;
}

function render() {
  if (!gallery) return;
  const allPhotos = gallery.posts.flatMap(photos);
  const visiblePhotos = post => photos(post).filter(photo =>
    photo.asset.userExcluded === true ? controlMode : filter === "all" || photo.result?.state === "candidate");
  const navEntries = newestFirst(gallery.posts).map(post => ({ post, photos: visiblePhotos(post) }))
    .filter(entry => entry.photos.length);
  updateMonthNavigation(navEntries);
  const datePosts = newestFirst(gallery.posts.filter(post => ui.date.value === "all" || dateKey(post.publishedAt) === ui.date.value));
  const datePhotos = datePosts.flatMap(photos);
  document.querySelector("#count-all").textContent = datePhotos.filter(photo => controlMode || photo.asset.userExcluded !== true).length;
  document.querySelector("#count-candidate").textContent = datePhotos.filter(photo => photo.asset.userExcluded !== true && photo.result?.state === "candidate").length;
  const displayed = navEntries.filter(entry => ui.date.value === "all" || dateKey(entry.post.publishedAt) === ui.date.value);
  const firstPostByMonth = new Map();
  for (const { post } of navEntries) {
    const date = dateKey(post.publishedAt);
    // Entries are newest first, so the final post stored for each month is
    // the earliest visible post in that month.
    if (date !== "unknown") firstPostByMonth.set(date.slice(0, 7), post.id);
  }
  const latestPostId = navEntries[0]?.post.id;
  ui.content.replaceChildren();
  if (displayed.length) {
    const list = element("div", "post-list");
    displayed.forEach(entry => {
      const date = dateKey(entry.post.publishedAt);
      const month = date === "unknown" ? null : date.slice(0, 7);
      const monthAnchorId = month && firstPostByMonth.get(month) === entry.post.id ? `month-${month}` : null;
      list.append(postCard(entry.post, entry.photos, monthAnchorId, entry.post.id === latestPostId));
    });
    ui.content.append(list);
  } else ui.content.append(emptyState(allPhotos.length));
}

function validCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function appendRunCounts(container, status) {
  const counts = status.counts || {};
  const photoLabels = { inspectedPhotos: "本次新檢查", candidatePhotos: "本次候選", noMatchPhotos: "人臉比對未命中", noFacePhotos: "未偵測到人臉", likedPhotos: "照片新增讚" };
  const photoText = Object.entries(photoLabels).filter(([key]) => validCount(counts[key]))
    .map(([key, label]) => `${label} ${counts[key]} 張`).join(" · ");
  if (photoText) container.append(element("p", "job-counts", photoText));
  const cachedPosts = validCount(counts.cachedPosts) ? counts.cachedPosts : Array.isArray(status.posts)
    ? status.posts.filter(post => post && (post.checkMode === "cached" || post.reason === "already_processed")).length : null;
  const postCounts = [["本次處理", counts.processedPosts], ["沿用先前結果", cachedPosts], ["略過", counts.skippedPosts], ["失敗", counts.failedPosts]];
  const postText = postCounts.filter(([, count]) => validCount(count)).map(([label, count]) => `${label} ${count} 篇`).join(" · ");
  if (postText) container.append(element("p", "job-counts", postText));
}

function updatePostAudits() {
  const status = gallery.jobStatus || {};
  const posts = newestFirst(Array.isArray(status.posts) ? status.posts.filter(post => post && typeof post === "object" && !["out_of_range", "missing_date"].includes(post.reason)) : []);
  ui.postAudits.replaceChildren();
  if (!posts.length) {
    ui.postAudits.append(element("p", "audit-note", "尚無逐篇照片核對紀錄。本機保留照片的數量無法確認原貼文是否全部檢查。"));
    return;
  }
  const discoveryIncomplete = status.discovery?.state === "partial" || ["scan_limit", "feed_stalled", "missing_date"].includes(status.reason);
  const auditNote = status.discoveryMode === "direct_post"
    ? "以下是指定貼文的照片檢查紀錄；本次沒有掃描粉專動態列表。"
    : discoveryIncomplete
      ? "貼文列表掃描尚未完整，所選日期範圍尚未完整。逐篇照片比對完成不代表已取得範圍內所有貼文。"
      : "以下紀錄列出本次取得的貼文；逐篇照片比對完成不代表已取得日期範圍內所有貼文。";
  ui.postAudits.append(element("p", "audit-note", auditNote));
  const wrapper = element("div", "audit-table-scroll");
  wrapper.setAttribute("tabindex", "0");
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-label", "逐篇照片檢查紀錄，可水平捲動");
  const table = element("table", "audit-table");
  const caption = element("caption", "visually-hidden", "本次取得貼文的照片總數與比對紀錄");
  const head = element("thead");
  const headings = element("tr");
  ["貼文日期／原貼文", "照片總數", "已比對", "候選", "本機保留", "檢查狀態"].forEach(label => {
    const cell = element("th", "", label);
    cell.setAttribute("scope", "col");
    headings.append(cell);
  });
  head.append(headings);
  const body = element("tbody");
  posts.forEach(post => {
    const row = element("tr");
    const source = element("td", "audit-source");
    const date = dateKey(post.publishedAt);
    source.append(element("span", "audit-date", date === "unknown" ? "日期未核對" : dateLabel(date)));
    source.append(element("span", "audit-title", post.title || "照片貼文"));
    const link = sourceLink(post.sourceUrl);
    if (link) source.append(link);
    row.append(source);
    const attachments = post.attachments || {};
    const attachmentComplete = attachments.state === "complete" && attachments.method === "explicit_limit_and_pagination"
      && attachments.requestedLimit === 100 && validCount(post.photoCount) && attachments.photoCount === post.photoCount;
    const candidateCount = validCount(post.candidatePhotoCount) ? post.candidatePhotoCount : post.historicalCandidatePhotoCount;
    const retainedPosts = typeof post.galleryPostId === "string" ? gallery.posts.filter(item => item.id === post.galleryPostId) : [];
    const retainedCount = retainedPosts.length === 1 ? photos(retainedPosts[0]).length : post.retainedPhotoCount;
    const values = [attachmentComplete ? post.photoCount : null, post.inspectedPhotoCount, candidateCount, retainedCount];
    values.forEach(value => row.append(element("td", "audit-number", validCount(value) ? value : "未核對")));
    const check = element("td", "audit-check");
    const mode = post.checkMode === "cached" ? "沿用先前結果" : post.checkMode === "fresh" ? "本次檢查" : "檢查方式未核對";
    const fullyChecked = attachmentComplete && validCount(post.inspectedPhotoCount) && post.inspectedPhotoCount === post.photoCount
      && ["fresh", "cached"].includes(post.checkMode) && typeof post.checkedAt === "string" && Number.isFinite(Date.parse(post.checkedAt));
    const result = attachments.state === "incomplete" ? "附件尚未完整" : fullyChecked ? "全部照片已比對"
      : attachmentComplete ? "尚未全部比對" : "照片總數未核對";
    check.append(element("span", "", `${mode} · ${result}`));
    if (typeof post.checkedAt === "string" && Number.isFinite(Date.parse(post.checkedAt))) {
      check.append(element("span", "audit-time", `${runDateFormatter.format(new Date(post.checkedAt))}（台灣時間）`));
    } else check.append(element("span", "audit-time", "檢查時間未核對"));
    row.append(check);
    body.append(row);
  });
  table.append(caption, head, body);
  wrapper.append(table);
  ui.postAudits.append(wrapper);
}

function updateJobStatus() {
  ui.schedule.textContent = "每天 20:00（台灣時間）";
  const status = gallery.jobStatus;
  ui.jobStatus.replaceChildren();
  ui.jobStatus.hidden = !status || typeof status !== "object";
  if (ui.jobStatus.hidden) return;
  const states = { complete: "最近一次流程已完成", partial: "最近一次流程部分完成", blocked: "最近一次流程受阻", failed: "最近一次流程失敗", error: "最近一次流程失敗", running: "照片流程進行中", idle: "等待每日照片流程", skipped: "最近一次流程已略過", locked: "已有照片流程正在執行" };
  ui.jobStatus.append(element("strong", "", states[status.state] || "每日照片流程狀態"));
  const mode = status.discoveryMode === "direct_post" ? "指定貼文直取" : status.discoveryMode === "replay" ? "觀察紀錄重播" : status.discoveryMode === "live" ? "即時瀏覽器掃描" : "發現方式未提供";
  ui.jobStatus.append(element("p", "job-mode", `${status.simulated === true ? "模擬執行" : "正式執行"} · ${mode}`));
  appendRunCounts(ui.jobStatus, status);
  if (typeof status.message === "string" && status.message) ui.jobStatus.append(element("p", "", status.message));
  if (typeof status.lastRunAt === "string" && Number.isFinite(Date.parse(status.lastRunAt))) {
    ui.jobStatus.append(element("p", "job-time", `最近執行：${runDateFormatter.format(new Date(status.lastRunAt))}（台灣時間）`));
  }
}

function updateMetadata() {
  const currentDate = ui.date.value;
  ui.date.replaceChildren(new Option("所有日期", "all"));
  const dates = [...new Set(gallery.posts.filter(post => photos(post).length).map(post => dateKey(post.publishedAt)))].sort((a, b) => a === "unknown" ? 1 : b === "unknown" ? -1 : b.localeCompare(a));
  dates.forEach(date => ui.date.append(new Option(dateLabel(date), date)));
  ui.date.value = dates.includes(currentDate) ? currentDate : "all";
  const fetchStatus = gallery.fetchStatus || {};
  ui.status.replaceChildren();
  ui.status.hidden = !controlMode || (!fetchStatus.message && fetchStatus.state !== "blocked");
  if (!ui.status.hidden) {
    ui.status.append(element("strong", "", fetchStatus.state === "blocked" ? "Facebook 照片取得尚未成功" : "照片匯入狀態"));
    if (typeof fetchStatus.message === "string") ui.status.append(element("p", "", fetchStatus.message));
    if (fetchStatus.state === "blocked") ui.status.append(element("p", "", "照片取得流程尚未完成，已保留的本機照片仍可查看。"));
  }
  updateJobStatus();
  updatePostAudits();
  const source = sourceLink(gallery.sourcePage, "小鹿托嬰中心 ↗");
  if (source) { source.id = "source-page"; document.querySelector("#source-page").replaceWith(source); }
}

async function loadGallery() {
  const requestId = ++galleryRequestId;
  ui.refresh.disabled = true;
  ui.content.setAttribute("aria-busy", "true");
  ui.error.hidden = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch("/api/gallery", { cache: "no-store", signal: controller.signal });
    if (requestId !== galleryRequestId) return;
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(`本機服務回應 HTTP ${response.status}${typeof detail.error === "string" ? `：${detail.error}` : ""}`);
    }
    const data = await response.json();
    if (requestId !== galleryRequestId) return;
    if (data.version !== 1 || !Array.isArray(data.posts)) throw new Error("相簿資料格式不符合版本 1");
    gallery = { ...data, posts: data.posts.filter(post => post && typeof post === "object" && post.kind === "photos") };
    updateMetadata();
    setFilter(filter);
    render();
  } catch (error) {
    if (requestId !== galleryRequestId) return;
    const detail = error.name === "AbortError" ? "讀取超過 15 秒" : error.message;
    ui.error.textContent = `無法讀取本機相簿：${detail}。請確認本機服務已啟動，再按「重新讀取本機資料」。${gallery ? "下方保留上次成功讀取的內容，可能不是最新狀態。" : ""}`;
    ui.error.hidden = false;
    if (!gallery) {
      ui.content.replaceChildren(element("div", "loading-state", "本機服務連線成功後，這裡會顯示候選照片與已保留的照片。"));
    }
  } finally {
    clearTimeout(timeout);
    if (requestId === galleryRequestId) {
      ui.refresh.disabled = false;
      ui.content.setAttribute("aria-busy", "false");
    }
  }
}

function setManualBusy(busy) {
  manualBusy = busy;
  const disabled = busy || !manualOptions;
  ui.fetchControls.disabled = disabled;
  [ui.fetchPreset, ui.fetchStart, ui.fetchEnd, ui.fetchSubmit].forEach(control => { control.disabled = disabled; });
  ui.manualPostUrl.disabled = busy;
  ui.manualPostSubmit.disabled = busy;
  ui.manualPostSubmit.textContent = busy ? "抓取進行中…" : "抓取指定貼文";
  ui.manualPostForm.setAttribute("aria-busy", String(busy));
  ui.fetchForm.setAttribute("aria-busy", String(busy));
  ui.fetchSubmit.textContent = busy ? (manualRun?.state === "running" ? "抓取進行中…" : "確認抓取狀態…") : "立即抓取貼文";
}

function showManualMessage(message) {
  ui.manualStatus.hidden = false;
  ui.manualStatus.replaceChildren(element("strong", "", message));
}

function updateFetchRange() {
  ui.fetchRange.textContent = `抓取日期：${ui.fetchStart.value} 至 ${ui.fetchEnd.value}（台灣時間，含起訖日）`;
}

function selectFetchPreset() {
  if (manualBusy || !manualOptions) return;
  const preset = manualOptions.presets.find(preset => preset.value === ui.fetchPreset.value);
  if (preset) {
    ui.fetchStart.value = preset.startDate;
    ui.fetchEnd.value = preset.endDate;
  }
  updateFetchRange();
}

async function manualRequest(url, init = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, { cache: "no-store", ...init, signal: controller.signal });
    const data = await response.json();
    return { response, data };
  } finally { clearTimeout(timeout); }
}

function scheduleManualPoll() {
  clearTimeout(manualPollTimer);
  manualPollTimer = setTimeout(() => { manualPollTimer = null; reconcileManualStatus(); }, 2000);
}

function appendManualProgress(container, status) {
  const progress = status.progress;
  if (status.state !== "running" || !progress || typeof progress !== "object") return;
  const titles = {
    preparing: "準備本機辨識",
    opening_post: "開啟指定貼文",
    scanning: "掃描貼文列表",
    resolving: "讀取貼文資訊",
    collecting: "讀取相簿照片",
    downloading: "下載照片",
    recognizing: "辨識照片",
    saving: "保存照片與結果",
    finalizing: "整理抓取結果",
  };
  const title = titles[progress.phase];
  if (!title) return;
  const panel = element("div", "fetch-progress");
  panel.setAttribute("aria-live", "off");
  panel.append(element("strong", "fetch-progress-title", title));

  let detail = "工作正在進行，請稍候。";
  if (progress.phase === "opening_post") detail = "直接載入該貼文，不掃描動態列表。";
  else if (progress.phase === "collecting") detail = `已讀取 ${validCount(progress.currentPhoto) ? progress.currentPhoto : 0} 張照片；正在確認相簿完整性。`;
  else if (progress.phase === "scanning" && validCount(progress.scrolls) && validCount(progress.scrollLimit)) {
    detail = `已捲動 ${progress.scrolls}/${progress.scrollLimit} 次 · 發現 ${validCount(progress.discoveredPosts) ? progress.discoveredPosts : 0} 篇照片貼文`;
  } else if (validCount(progress.totalPosts) && progress.totalPosts > 0) {
    const completed = Math.min(validCount(progress.completedPosts) ? progress.completedPosts : 0, progress.totalPosts);
    const current = Math.min(validCount(progress.currentPost) && progress.currentPost > 0
      ? progress.currentPost : completed + 1, progress.totalPosts);
    detail = `貼文 ${current}/${progress.totalPosts} · 已完成 ${completed}/${progress.totalPosts} 篇`;
    if (["downloading", "recognizing"].includes(progress.phase) &&
        validCount(progress.currentPhoto) && validCount(progress.currentPhotoTotal) && progress.currentPhotoTotal > 0) {
      detail += ` · 照片 ${Math.min(progress.currentPhoto, progress.currentPhotoTotal)}/${progress.currentPhotoTotal} 張`;
    }
  } else if (progress.phase === "resolving") detail = "正在整理已發現的貼文。";

  panel.append(element("span", "fetch-progress-detail", detail));
  const track = element("div", "fetch-progress-track");
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", "貼文抓取進度");
  const fill = element("div", "fetch-progress-fill");
  if (["scanning", "preparing", "opening_post", "collecting"].includes(progress.phase) || !validCount(progress.totalPosts) || progress.totalPosts === 0) {
    track.setAttribute("aria-valuetext", "進行中");
    fill.classList.add("indeterminate");
  } else {
    const completed = Math.min(validCount(progress.completedPosts) ? progress.completedPosts : 0, progress.totalPosts);
    let withinPost = 0;
    if (["downloading", "recognizing"].includes(progress.phase) &&
        validCount(progress.currentPhoto) && validCount(progress.currentPhotoTotal) && progress.currentPhotoTotal > 0) {
      const photoRatio = Math.min(progress.currentPhoto / progress.currentPhotoTotal, 1);
      withinPost = progress.phase === "downloading" ? 0.05 + 0.45 * photoRatio : 0.5 + 0.45 * photoRatio;
    } else if (progress.phase === "saving") withinPost = 0.98;
    else if (progress.phase === "resolving") withinPost = 0.05;
    const ratio = Math.min(0.99, (completed + withinPost) / progress.totalPosts);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", String(progress.totalPosts));
    track.setAttribute("aria-valuenow", String(Math.min(progress.totalPosts, completed + withinPost)));
    track.setAttribute("aria-valuetext", `${Math.round(ratio * 100)}%`);
    fill.style.width = `${Math.round(ratio * 100)}%`;
  }
  track.append(fill);
  panel.append(track);
  container.append(panel);
}

function applyManualStatus(status, refreshGallery = true) {
  const labels = { idle: "", running: "貼文抓取進行中", complete: "貼文抓取已完成", partial: "貼文抓取部分完成", blocked: "貼文抓取受阻", failed: "貼文抓取失敗" };
  if (!status || !Object.hasOwn(labels, status.state)) throw new Error("Invalid fetch status");
  const wasBusy = manualBusy;
  manualRun = status;
  clearTimeout(manualPollTimer);
  manualPollTimer = null;
  setManualBusy(status.state === "running");
  ui.manualStatus.replaceChildren();
  ui.manualStatus.hidden = status.state === "idle";
  if (status.state !== "idle") {
    ui.manualStatus.append(element("strong", "", labels[status.state]));
    if (status.range && /^\d{4}-\d{2}-\d{2}$/.test(status.range.startDate) && /^\d{4}-\d{2}-\d{2}$/.test(status.range.endDate)) {
      ui.manualStatus.append(element("p", "", `${status.range.startDate} 至 ${status.range.endDate}（台灣時間，含起訖日）`));
    } else if (status.targetType === "post") ui.manualStatus.append(element("p", "", "直接處理指定貼文；不掃描動態列表。"));
    if (typeof status.message === "string" && status.message) ui.manualStatus.append(element("p", "", status.message));
    appendManualProgress(ui.manualStatus, status);
    appendRunCounts(ui.manualStatus, status);
    if (typeof status.finishedAt === "string" && Number.isFinite(Date.parse(status.finishedAt))) {
      ui.manualStatus.append(element("p", "job-time", `結束時間：${runDateFormatter.format(new Date(status.finishedAt))}（台灣時間）`));
    }
  }
  if (status.state === "running") scheduleManualPoll();
  else if (refreshGallery && wasBusy && status.state !== "idle") loadGallery();
  if (manualOptionsFailed) {
    if (status.state === "idle") showManualMessage("無法讀取抓取設定，請重新整理頁面後再試。");
    else ui.manualStatus.append(element("p", "", "無法讀取抓取設定，請重新整理頁面後再試。"));
  }
}

async function reconcileManualStatus(refreshGallery = true) {
  try {
    const { response, data } = await manualRequest("/api/manual-fetch/status");
    if (!response.ok) throw new Error("Status unavailable");
    applyManualStatus(data, refreshGallery);
  } catch {
    setManualBusy(true);
    showManualMessage("暫時無法確認抓取狀態，正在重新連線。請稍候。");
    scheduleManualPoll();
  }
}

async function refreshManualOptions(useDefault = false) {
  const selected = useDefault ? "3d" : ui.fetchPreset.value;
  const { response, data } = await manualRequest("/api/manual-fetch/options");
  if (!response.ok || data.timeZone !== "Asia/Taipei" || !Array.isArray(data.presets) || !data.defaultRange || !data.minDate || !data.maxDate) throw new Error("Options unavailable");
  manualOptions = data;
  manualOptionsFailed = false;
  ui.fetchPreset.replaceChildren(...data.presets.map(preset => new Option(preset.label, preset.value)), new Option("自訂日期", "custom"));
  const preset = data.presets.find(preset => preset.value === selected);
  ui.fetchPreset.value = preset ? selected : "custom";
  const range = useDefault ? data.defaultRange : preset;
  if (range) {
    ui.fetchStart.value = range.startDate;
    ui.fetchEnd.value = range.endDate;
  }
  [ui.fetchStart, ui.fetchEnd].forEach(input => { input.min = data.minDate; input.max = data.maxDate; });
  updateFetchRange();
}

async function initializeManualFetch() {
  setManualBusy(true);
  await Promise.all([
    (async () => {
      try {
        await refreshManualOptions(true);
        setManualBusy(manualBusy);
      } catch {
        manualOptionsFailed = true;
        setManualBusy(manualRun?.state === "running");
        if (manualRun && manualRun.state !== "idle") ui.manualStatus.append(element("p", "", "無法讀取抓取設定，請重新整理頁面後再試。"));
        else showManualMessage("無法讀取抓取設定，請重新整理頁面後再試。");
      }
    })(),
    reconcileManualStatus(),
  ]);
}

async function submitManualFetch(event) {
  event.preventDefault();
  if (manualBusy || !manualOptions) return;
  setManualBusy(true);
  ui.fetchSubmit.textContent = "確認最新日期…";
  showManualMessage("正在確認最新的抓取日期…");
  try {
    await refreshManualOptions();
  } catch {
    setManualBusy(false);
    showManualMessage("無法確認最新抓取日期，尚未送出抓取請求。請稍後再試。");
    return;
  }
  const startDate = ui.fetchStart.value, endDate = ui.fetchEnd.value;
  const validDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value) && value >= manualOptions.minDate && value <= manualOptions.maxDate;
  if (!validDate(startDate) || !validDate(endDate) || startDate > endDate) {
    setManualBusy(false);
    showManualMessage("請確認抓取日期：起日須早於或等於迄日，範圍最多往前 3 個月。");
    return;
  }
  ui.fetchSubmit.textContent = "正在送出…";
  showManualMessage("正在送出貼文抓取請求…");
  try {
    const { response, data } = await manualRequest("/api/manual-fetch", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ startDate, endDate }),
    });
    if (response.status === 400) {
      setManualBusy(false);
      showManualMessage("無法開始抓取，請確認日期範圍後再試。最多可往前 3 個月。");
      return;
    }
    if (!response.ok && response.status !== 409) throw new Error("Fetch request unavailable");
    applyManualStatus(data.status && typeof data.status === "object" ? data.status : data);
  } catch {
    setManualBusy(true);
    showManualMessage("暫時無法確認抓取是否已開始，正在重新連線。請稍候。");
    scheduleManualPoll();
  }
}

async function submitManualPost(event) {
  event.preventDefault();
  if (manualBusy || !controlMode || !ui.manualPostForm.reportValidity()) return;
  setManualBusy(true);
  ui.manualPostSubmit.textContent = "正在送出…";
  showManualMessage("正在開啟指定貼文，不會掃描動態列表…");
  try {
    const { response, data } = await manualRequest("/api/manual-fetch/post?ctrl=1", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: ui.manualPostUrl.value }),
    });
    if (response.status === 409 && data.status) {
      applyManualStatus(data.status);
      return;
    }
    if (!response.ok) {
      setManualBusy(false);
      showManualMessage(typeof data.error === "string" ? data.error : "無法開始指定貼文抓取，請確認網址後重試。");
      return;
    }
    applyManualStatus(data);
  } catch {
    setManualBusy(true);
    showManualMessage("暫時無法確認指定貼文抓取是否已開始，正在重新連線。請稍候。");
    scheduleManualPoll();
  }
}

function setManualPhotoBusy(busy) {
  manualPhotoBusy = busy;
  ui.manualPhotoUrl.disabled = busy;
  ui.manualPhotoSubmit.disabled = busy;
  ui.manualPhotoForm.setAttribute("aria-busy", String(busy));
  ui.manualPhotoSubmit.textContent = busy ? "加入中…" : "手動加入照片";
}

function applyManualPhotoStatus(status) {
  const labels = { idle: "", running: "正在手動加入照片", complete: "手動加入完成", blocked: "手動加入受阻", failed: "手動加入失敗" };
  if (!status || !Object.hasOwn(labels, status.state)) throw new Error("Invalid manual photo status");
  const wasBusy = manualPhotoBusy;
  clearTimeout(manualPhotoPollTimer);
  manualPhotoPollTimer = null;
  setManualPhotoBusy(status.state === "running");
  ui.manualPhotoStatus.replaceChildren();
  ui.manualPhotoStatus.hidden = status.state === "idle";
  if (status.state !== "idle") {
    ui.manualPhotoStatus.append(element("strong", "", labels[status.state]));
    if (typeof status.message === "string" && status.message) ui.manualPhotoStatus.append(element("p", "", status.message));
    if (typeof status.finishedAt === "string" && Number.isFinite(Date.parse(status.finishedAt))) {
      ui.manualPhotoStatus.append(element("p", "job-time", `結束時間：${runDateFormatter.format(new Date(status.finishedAt))}（台灣時間）`));
    }
  }
  if (status.state === "running") {
    manualPhotoPollTimer = setTimeout(reconcileManualPhotoStatus, 900);
  } else if (wasBusy && status.state === "complete") {
    ui.manualPhotoUrl.value = "";
    loadGallery();
  }
}

async function reconcileManualPhotoStatus() {
  try {
    const { response, data } = await manualRequest("/api/manual-photo/status?ctrl=1");
    if (!response.ok) throw new Error("Status unavailable");
    applyManualPhotoStatus(data);
  } catch {
    setManualPhotoBusy(true);
    ui.manualPhotoStatus.hidden = false;
    ui.manualPhotoStatus.replaceChildren(element("strong", "", "暫時無法確認手動加入狀態，正在重新連線。"));
    manualPhotoPollTimer = setTimeout(reconcileManualPhotoStatus, 1200);
  }
}

async function initializeManualPhoto() {
  if (!controlMode) return;
  await reconcileManualPhotoStatus();
}

async function submitManualPhoto(event) {
  event.preventDefault();
  if (!controlMode || manualPhotoBusy) return;
  setManualPhotoBusy(true);
  ui.manualPhotoStatus.hidden = false;
  ui.manualPhotoStatus.replaceChildren(element("strong", "", "正在確認粉專來源並加入照片…"));
  try {
    const { response, data } = await manualRequest("/api/manual-photo?ctrl=1", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: ui.manualPhotoUrl.value }),
    });
    if (response.status === 409 && data.status) {
      applyManualPhotoStatus(data.status);
      return;
    }
    if (!response.ok) {
      setManualPhotoBusy(false);
      ui.manualPhotoStatus.replaceChildren(element("strong", "", typeof data.error === "string" ? data.error : "無法手動加入照片，請確認網址後重試。"));
      return;
    }
    applyManualPhotoStatus(data);
  } catch {
    ui.manualPhotoStatus.replaceChildren(element("strong", "", "暫時無法確認手動加入是否已開始，正在重新連線。"));
    manualPhotoPollTimer = setTimeout(reconcileManualPhotoStatus, 900);
  }
}

document.querySelectorAll("[data-filter]").forEach(button => button.addEventListener("click", () => { setFilter(button.dataset.filter); render(); }));
ui.date.addEventListener("change", render);
ui.refresh.addEventListener("click", loadGallery);
ui.fetchPreset.addEventListener("change", selectFetchPreset);
[ui.fetchStart, ui.fetchEnd].forEach(input => input.addEventListener("change", () => {
  if (manualBusy) return;
  ui.fetchPreset.value = "custom";
  updateFetchRange();
}));
ui.fetchForm.addEventListener("submit", submitManualFetch);
ui.manualPostForm.addEventListener("submit", submitManualPost);
ui.manualPhotoForm.addEventListener("submit", submitManualPhoto);
loadGallery();
initializeManualFetch();
initializeManualPhoto();
