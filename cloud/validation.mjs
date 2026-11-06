export class HttpError extends Error {
  constructor(statusCode, message) { super(message); this.statusCode = statusCode; }
}
const PAGE_ID = '100063963161862';
const invalid = message => { throw new HttpError(400, message); };
const secretQuery = /^(access_token|appsecret_proof|oauth_token|token|authorization)$/i;
const validDate = value => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(value + 'T00:00:00Z');
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
};
function subtract(value, months = 0, days = 0) {
  const date = new Date(value + 'T00:00:00Z'), day = date.getUTCDate();
  date.setUTCDate(1); date.setUTCMonth(date.getUTCMonth() - months);
  date.setUTCDate(Math.min(day, new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate()) - days);
  return date.toISOString().slice(0, 10);
}
export function createOptions(now = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now).map(p => [p.type, p.value]));
  const today = `${parts.year}-${parts.month}-${parts.day}`;
  const presets = [
    { value: '3d', label: '近 3 天', startDate: subtract(today, 0, 2) },
    { value: '7d', label: '近 7 天', startDate: subtract(today, 0, 6) },
    { value: '14d', label: '近 14 天', startDate: subtract(today, 0, 13) },
    { value: '1m', label: '近 1 個月', startDate: subtract(today, 1) },
    { value: '3m', label: '近 3 個月', startDate: subtract(today, 3) },
  ].map(p => ({ ...p, endDate: today }));
  return { timeZone: 'Asia/Taipei', today, minDate: subtract(today, 3), maxDate: today, defaultRange: { startDate: presets[0].startDate, endDate: today }, presets };
}
export function validateRange(input, now = new Date()) {
  const options = createOptions(now);
  if (!input || Array.isArray(input) || Object.keys(input).some(key => !['startDate', 'endDate'].includes(key)) ||
      !validDate(input.startDate) || !validDate(input.endDate) || input.startDate > input.endDate || input.startDate < options.minDate || input.endDate > options.maxDate) invalid('請選擇近三個月內的有效日期。');
  return { startDate: input.startDate, endDate: input.endDate, timeZone: options.timeZone };
}
function facebookUrl(value) {
  if (typeof value !== 'string' || value.trim() !== value || value.length > 4096) invalid('請提供有效的 Facebook 網址。');
  let url; try { url = new URL(value); } catch { invalid('請提供有效的 Facebook 網址。'); }
  if (url.protocol !== 'https:' || !['facebook.com', 'www.facebook.com', 'm.facebook.com'].includes(url.hostname) ||
      url.username || url.password || url.port || url.hash || [...url.searchParams.keys()].some(key => secretQuery.test(key))) invalid('請提供有效的 Facebook 網址。');
  return url;
}
export function normalizePostUrl(value) {
  const url = facebookUrl(value);
  if (['/permalink.php', '/story.php'].includes(url.pathname)) {
    const stories = url.searchParams.getAll('story_fbid'), pages = url.searchParams.getAll('id');
    if (stories.length !== 1 || pages.length !== 1 || !/^[A-Za-z0-9_.:-]{1,200}$/.test(stories[0]) || pages[0] !== PAGE_ID) invalid('請貼上小鹿托嬰中心的貼文網址。');
    return `https://www.facebook.com${url.pathname}?${new URLSearchParams({ story_fbid: stories[0], id: pages[0] })}`;
  }
  if (/^\/(?:[^/]+\/)?posts\/[A-Za-z0-9_.:-]+\/?$/.test(url.pathname) || /^\/share\/p\/[A-Za-z0-9_.:-]+\/?$/.test(url.pathname)) return `https://www.facebook.com${url.pathname.replace(/\/$/, '')}`;
  invalid('請貼上小鹿托嬰中心的貼文網址。');
}
export function photoIdFromUrl(value) {
  const url = facebookUrl(value), ids = url.searchParams.getAll('fbid');
  if (!['/photo', '/photo/', '/photo.php'].includes(url.pathname) || ids.length !== 1 || !/^\d{1,50}$/.test(ids[0])) invalid('請貼上含有 fbid 的 Facebook 照片頁網址。');
  return ids[0];
}
export function validateFeedback(input) {
  if (!input || Object.keys(input).sort().join(',') !== 'assetId,excluded,postId' ||
      !/^[A-Za-z0-9_-]{1,200}$/.test(input.postId || '') || !/^[A-Za-z0-9_-]{1,200}$/.test(input.assetId || '') || typeof input.excluded !== 'boolean') invalid('請提供有效的照片標記。');
  return input;
}
