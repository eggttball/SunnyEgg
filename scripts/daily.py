#!/usr/bin/env python3
"""Photo-only daily batch: private download, local matching, candidate promotion."""
import argparse
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid
from zoneinfo import ZoneInfo

from common import (MediaError, SOURCE_PAGE, data_directory, finite_number, load_manifest,
                    manifest_lock, safe_data_path, save_manifest)
import facebook
import feedback
from feedback import identity_canonicals, identity_groups, source_photo_id
import media
import recognize

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = {'cron': '0 20 * * *', 'timeZone': 'Asia/Taipei'}
TAIPEI = ZoneInfo('Asia/Taipei')
POST_FIELDS = 'id,message,created_time,permalink_url,from,attachments.limit(100){media,target,type,subattachments.limit(100){media,target,type}}'
MAX_ATTACHMENT_PAGES = 100
# Locally verified Graph lookup alias for this project's one configured source page.
VERIFIED_POST_PAGE_ALIASES = {'864287026968664': '100063963161862'}
MESSAGES = {
    'batch_locked': '已有每日照片工作執行中，本次未啟動。',
    'configuration': '請確認明確指定的本機 Facebook 設定與每日工作設定。',
    'invalid_date_range': '請同時指定有效的開始與結束日期；範圍須介於台北今天往前 3 個月到今天。',
    'missing_date': 'Facebook 未提供可驗證的貼文發布日期；已略過，日期範圍檢查尚未完整。',
    'recognition_prerequisites': '本機參考照片或辨識模型尚未就緒；未新增照片。',
    'feedback_unavailable': '本機誤判標記或負例照片無法讀取；請確認本機資料完整後重試。',
    'feedback_changed': '誤判標記已更新，本次照片工作已暫停；請重新抓取，以新的標記進行比對。',
    'invalid_discovery': '照片發現資料格式或來源網址無效；未使用此資料。',
    'browser_unavailable': '專用瀏覽器發現尚未完成，請確認 Playwright 與專用設定檔。',
    'browser_dependency_missing': '尚未安裝 Playwright，請在 SunnyEgg 終端機執行 npm install 後重試。',
    'browser_missing': '尚未安裝 Chromium，請在 SunnyEgg 終端機執行 npx playwright install chromium 後重試。',
    'profile_missing': '尚未建立專用 Facebook 登入，請先執行專用瀏覽器登入指令並完成登入。',
    'profile_unavailable': '無法存取專用瀏覽器設定檔，請確認本機資料目錄與檔案權限。',
    'profile_in_use': '專用瀏覽器設定檔正在使用中，請先關閉整個專用瀏覽器後重試。',
    'browser_launch_failed': '專用瀏覽器啟動失敗，請確認本機瀏覽器安裝後重試。',
    'facebook_navigation_failed': '專用瀏覽器已開啟，但 Facebook 連線失敗，請確認網路連線後重試。',
    'login_cancelled': '專用瀏覽器登入已取消，請重新執行登入指令並完成 Facebook 登入。',
    'invalid_arguments': '專用瀏覽器指令參數無效，請確認照片工作設定後重試。',
    'login_required': '專用瀏覽器登入已失效，需在本機重新登入。',
    'scan_limit': '瀏覽器已達掃描上限；已處理觀察到的照片，發現範圍尚未完整。',
    'feed_stalled': '貼文列表停止載入；已處理觀察到的照片，日期範圍尚未完整。',
    'page_unavailable': 'Facebook 專頁目前無法讀取，請在本機確認專頁是否可開啟。',
    'feed_unavailable': 'Facebook 貼文列表目前無法讀取，請稍後重試。',
    'permission': 'Facebook 權限不足，已保留原有相簿。',
    'credentials': 'Facebook 存取權杖無效或已過期，請在本機更新。',
    'remote_failure': 'Facebook API 讀取失敗，已保留原有相簿。',
    'rate_limit': 'Facebook API 暫時達到讀取上限；已保留完成結果並停止後續請求，請稍後重試。',
    'unsafe_url': 'Facebook 回應包含不允許的連線網址；未下載。',
    'invalid_response': 'Facebook API 回應不完整或格式無效；未匯入此貼文。',
    'pagination_limit': '附件未能完整讀取；未匯入此貼文。',
    'pagination_cycle': '附件分頁循環；未匯入此貼文。',
    'wrong_page': '照片父貼文不屬於設定的粉專；未匯入。',
    'not_photos_only': '父貼文包含影片或不支援附件；未匯入。',
    'post_unavailable': '指定貼文無法載入，或頁面尚未登入可查看。',
    'post_no_photos': '指定貼文沒有可辨識的照片。',
    'source_page_unverified': '無法確認指定貼文屬於小鹿托嬰中心；未下載照片。',
    'album_incomplete': 'Facebook 相簿未能完整瀏覽；未匯入不完整照片。',
    'album_limit': '相簿照片超過單次處理上限；未匯入不完整照片。',
    'invalid_post_url': '請貼上小鹿托嬰中心 Facebook 的貼文網址。',
    'photo_unavailable': 'Facebook 未提供可下載的原生照片；未匯入。',
    'unsupported_media': '原生照片格式不受支援；未匯入。',
    'download_failure': '原生照片下載未完成；未匯入此貼文。',
    'recognition_failure': '照片本機辨識未完成；未匯入此貼文。',
    'import_failure': '候選照片未通過本機匯入驗證。',
    'local_storage': '無法安全更新本機工作紀錄，請確認 data 目錄與檔案權限。',
    'reaction_failure': '候選照片已保存，對應照片按讚尚未確認成功。',
}
BROWSER_ERROR_REASONS = {
    'browser_unavailable', 'browser_dependency_missing', 'browser_missing',
    'profile_missing', 'profile_unavailable', 'profile_in_use', 'browser_launch_failed',
    'facebook_navigation_failed', 'login_cancelled', 'invalid_arguments', 'login_required',
    'post_unavailable', 'post_no_photos', 'source_page_unverified', 'album_incomplete', 'album_limit',
}


class DailyError(MediaError):
    def __init__(self, reason):
        self.reason = reason if reason in MESSAGES else 'remote_failure'
        super().__init__(MESSAGES[self.reason])


def _reason(error, fallback):
    reason = getattr(error, 'reason', fallback)
    return reason if reason in MESSAGES else fallback


def _iso(value):
    try:
        date = datetime.fromisoformat(re.sub(r'([+-][0-9]{2})([0-9]{2})$', r'\1:\2', value.replace('Z', '+00:00')))
        if date.tzinfo is None:
            raise ValueError()
        return date.isoformat()
    except (ValueError, TypeError, AttributeError):
        raise DailyError('invalid_response') from None


def _date_window(start_date, end_date, clock):
    if start_date is None and end_date is None:
        return None
    try:
        if not all(isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value)
                   for value in (start_date, end_date)):
            raise ValueError()
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        today = clock.astimezone(TAIPEI).date()
        year, month = divmod(today.year * 12 + today.month - 1 - 3, 12)
        month += 1
        earliest = date(year, month, min(today.day, monthrange(year, month)[1]))
        if not earliest <= start <= end <= today:
            raise ValueError()
        return start, end
    except (ValueError, TypeError, OverflowError):
        raise DailyError('invalid_date_range') from None


def _source_url(value, reject_secrets=False):
    """Retain public Facebook identifiers, never arbitrary query data or fragments."""
    if reject_secrets:
        media.validate_source_url(value)
    else:
        value = facebook.checked_url(value)
    parts = urlsplit(value)
    if parts.hostname not in {'facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com'}:
        raise DailyError('unsafe_url')
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
             if key in {'id', 'story_fbid', 'fbid', 'set'} and re.fullmatch(r'[A-Za-z0-9_.:-]+', item)]
    result = urlunsplit(('https', 'www.facebook.com', parts.path, urlencode(query), ''))
    media.validate_source_url(result)
    return result


def _post_url(value, page):
    if not isinstance(value, str):
        raise DailyError('invalid_post_url')
    try:
        media.validate_source_url(value)
        parts = urlsplit(value)
        if parts.fragment:
            raise ValueError()
        if parts.path in {'/permalink.php', '/story.php'}:
            query = parse_qsl(parts.query, keep_blank_values=True)
            story_ids = [value for key, value in query if key == 'story_fbid']
            page_ids = [value for key, value in query if key == 'id']
            story_id = story_ids[0] if len(story_ids) == 1 else None
            page_id = page_ids[0] if len(page_ids) == 1 else None
            source_page_id = dict(parse_qsl(urlsplit(SOURCE_PAGE).query)).get('id')
            allowed_page_ids = {page, source_page_id, VERIFIED_POST_PAGE_ALIASES.get(page)}
            if (not isinstance(story_id, str) or re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', story_id) is None
                    or not isinstance(page_id, str) or not page_id.isdigit() or page_id not in allowed_page_ids):
                raise ValueError()
            path = parts.path
            query_string = urlencode({'story_fbid': story_id, 'id': page_id})
        elif re.fullmatch(r'/(?:[^/]+/)?posts/[A-Za-z0-9_.:-]+/?', parts.path):
            path, query_string = parts.path.rstrip('/'), ''
        elif re.fullmatch(r'/share/p/[A-Za-z0-9_.:-]+/?', parts.path):
            path, query_string = parts.path.rstrip('/'), ''
        else:
            raise ValueError()
        return urlunsplit(('https', 'www.facebook.com', path, query_string, ''))
    except (MediaError, ValueError, TypeError):
        raise DailyError('invalid_post_url') from None


def _read_json(path, limit=8 * 1024 * 1024):
    if path.is_symlink():
        raise DailyError('local_storage')
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('size')
    return json.loads(raw.decode('utf-8'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('number')))


def _private_directory(root, relative):
    directory = safe_data_path(root, relative)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _atomic_json(root, relative, value):
    target = safe_data_path(root, relative)
    parent = _private_directory(root, str(Path(relative).parent)) if '/' in relative else data_directory(root, create=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    descriptor, temporary = tempfile.mkstemp(prefix='.daily-', suffix='.json', dir=str(parent))
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _manual_progress(root, progress_id, phase, **values):
    if progress_id is None:
        return
    try:
        if str(uuid.UUID(progress_id)) != progress_id:
            return
        _atomic_json(root, 'runs/.manual-progress-' + progress_id + '.json', {'phase': phase, **values})
    except (MediaError, OSError, TypeError, ValueError):
        # Progress is advisory and must never interrupt photo processing.
        return


@contextmanager
def _batch_lock(root):
    data_directory(root, create=True)
    descriptor = os.open(str(safe_data_path(root, '.daily.lock')), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            raise DailyError('batch_locked') from None
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _configuration(root, env_file):
    values = facebook.load_environment_file(env_file) if env_file is not None else {}
    for key in facebook.ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    token, version, page = facebook.configured(values, 14, MAX_ATTACHMENT_PAGES)
    config_path = Path(root) / 'config/job.json'
    config = _read_json(config_path) if config_path.exists() else {}
    if not isinstance(config, dict) or config.get('version', 1) != 1 or not isinstance(config.get('autoLike', True), bool):
        raise DailyError('configuration')
    max_scrolls = config.get('maxScrolls', 12)
    if isinstance(max_scrolls, bool) or not isinstance(max_scrolls, int) or not 1 <= max_scrolls <= 100:
        raise DailyError('configuration')
    profile = config.get('browserProfile', 'data/browser-profile')
    if not isinstance(profile, str) or not profile.startswith('data/'):
        raise DailyError('configuration')
    profile = safe_data_path(root, profile[5:])
    if profile.relative_to(data_directory(root)).parts[0] in {'reference-child', 'models', 'media', 'runs', 'downloads'}:
        raise DailyError('configuration')
    return token, version, page, profile, config.get('autoLike', True), max_scrolls


def _browser(root, command, profile, target=None, max_scrolls=12, progress_id=None):
    argv = ['node', str(ROOT / 'scripts/browser-collector.mjs'), command, '--profile', str(profile)]
    if target is not None:
        argv.extend(['--photo-id' if command == 'react-photo' else '--url', target])
    if command == 'discover':
        argv.extend(['--max-scrolls', str(max_scrolls)])
        if progress_id is not None:
            argv.extend(['--progress-id', progress_id])
    elif command == 'collect-post-photos' and progress_id is not None:
        argv.extend(['--progress-id', progress_id])
    # The collector uses its own login profile and has no reason to inherit API credentials.
    environment = {key: value for key, value in os.environ.items() if key in {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT', 'WINDIR'}}
    try:
        result = subprocess.run(argv, cwd=str(root), env=environment, capture_output=True, text=True, timeout=300, check=False)
        if len(result.stdout) > 8 * 1024 * 1024:
            raise ValueError()
        body = json.loads(result.stdout)
        if not isinstance(body, dict):
            raise ValueError()
        # A structured blocked/partial result can legitimately have a nonzero exit code.
        if result.returncode and body.get('state') not in {'partial', 'blocked'}:
            raise ValueError()
        return body
    except (OSError, ValueError, subprocess.SubprocessError, UnicodeError):
        raise DailyError('reaction_failure' if command in {'react', 'react-photo'} else 'browser_unavailable') from None


def _safe_coverage(value):
    if not isinstance(value, dict):
        return None
    result = {}
    if value.get('state') in {'complete', 'partial', 'blocked'}:
        result['state'] = value['state']
    if value.get('reason') in MESSAGES:
        result['reason'] = value['reason']
    for key in ('scrolls', 'snapshots', 'observedPhotoPosts', 'observedVideos', 'loadWaitMs'):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            result[key] = number
    if isinstance(value.get('atBottom'), bool):
        result['atBottom'] = value['atBottom']
    if value.get('budget') in {'scroll_limit', 'time_limit', None}:
        result['budget'] = value.get('budget')
    scroll = value.get('scroll')
    if isinstance(scroll, dict):
        bounds = {}
        for key in ('top', 'viewport', 'height', 'bottomDistance'):
            number = finite_number(scroll.get(key))
            if number is not None and number >= 0:
                bounds[key] = number
        if scroll.get('container') in {'document', 'element'}:
            bounds['container'] = scroll['container']
        result['scroll'] = bounds
    return result


def _discovery(root, discovery_file, simulate, profile, max_scrolls, progress_id=None, post_url=None):
    if simulate and discovery_file is None:
        raise DailyError('invalid_discovery')
    try:
        if discovery_file is not None:
            body = _read_json(Path(discovery_file))
        elif post_url is not None:
            body = _browser(root, 'inspect-post', profile, target=post_url)
        elif progress_id is None:
            body = _browser(root, 'discover', profile, max_scrolls=max_scrolls)
        else:
            body = _browser(root, 'discover', profile, max_scrolls=max_scrolls, progress_id=progress_id)
        if not isinstance(body, dict) or body.get('version') != 1 or not isinstance(body.get('posts'), list) or len(body['posts']) > 500:
            raise ValueError()
        captured = _iso(body.get('capturedAt'))
        state = body.get('state', 'complete')
        if state not in {'complete', 'partial', 'blocked'}:
            raise ValueError()
        skipped = body.get('skippedVideos', 0)
        if isinstance(skipped, bool) or not isinstance(skipped, int) or skipped < 0:
            raise ValueError()
        posts = []
        for item in body['posts']:
            if not isinstance(item, dict):
                raise ValueError()
            if item.get('kind') in {'video', 'videos', 'reel'}:
                skipped += 1
                continue
            if item.get('kind') != 'photos' or not isinstance(item.get('photoId'), str) or not re.fullmatch(r'[0-9]+', item['photoId']):
                raise ValueError()
            posts.append({'photoId': item['photoId'], 'sourceUrl': _source_url(item.get('sourceUrl'), reject_secrets=True)})
        reason = body.get('reason')
        if reason not in MESSAGES:
            reason = 'browser_unavailable' if state == 'blocked' else 'scan_limit' if state == 'partial' else None
        mode = 'replay' if discovery_file is not None else 'direct_post' if post_url is not None else 'live'
        discovery = {'mode': mode, 'capturedAt': captured, 'state': state, 'reason': reason}
        coverage = _safe_coverage(body.get('coverage'))
        if coverage is not None:
            discovery['coverage'] = coverage
        return discovery, posts, skipped
    except DailyError as error:
        if discovery_file is None and error.reason in BROWSER_ERROR_REASONS:
            raise
        raise DailyError('invalid_discovery') from None
    except (MediaError, OSError, ValueError, TypeError, UnicodeError):
        raise DailyError('invalid_discovery') from None


def _browser_post_photo_item(root, profile, post_url, page, seed_photo_id, progress_id=None):
    """Resolve every photo from an authenticated browser album when Graph cannot read it."""
    body = _browser(root, 'collect-post-photos', profile, target=post_url, progress_id=progress_id)
    if not isinstance(body, dict) or body.get('version') != 1:
        raise DailyError('album_incomplete')
    if body.get('state') != 'complete':
        reason = body.get('reason')
        raise DailyError(reason if reason in MESSAGES else 'album_incomplete')
    page_id = body.get('postPageId')
    source_page_id = dict(parse_qsl(urlsplit(SOURCE_PAGE).query)).get('id')
    allowed_page_ids = {page, source_page_id, VERIFIED_POST_PAGE_ALIASES.get(page)}
    if not isinstance(page_id, str) or page_id not in allowed_page_ids:
        raise DailyError('wrong_page')
    photos = body.get('photos')
    if not isinstance(photos, list) or not photos or len(photos) > 200:
        raise DailyError('album_incomplete')
    identifiers, sources = [], {}
    for photo in photos:
        if not isinstance(photo, dict):
            raise DailyError('album_incomplete')
        identifier = photo.get('photoId')
        source = photo.get('sourceUrl')
        if not isinstance(identifier, str) or re.fullmatch(r'[0-9]+', identifier) is None or identifier in sources:
            raise DailyError('album_incomplete')
        try:
            source = facebook.checked_url(source)
        except facebook.FetchError:
            raise DailyError('unsafe_url') from None
        identifiers.append(identifier)
        sources[identifier] = source
    if seed_photo_id not in sources:
        raise DailyError('invalid_response')
    parts = urlsplit(post_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    post_key = query.get('story_fbid')
    if post_key is None:
        match = re.search(r'/(?:posts|share/p)/([^/]+)$', parts.path)
        post_key = match.group(1) if match else parts.path
    identity_key = hashlib.sha256((page_id + ':' + post_key).encode()).hexdigest()
    published_at = body.get('publishedAt')
    if published_at is not None:
        published_at = _iso(published_at)
    return {'sourceId': 'browser:' + identity_key, 'retrievalPostId': 'browser:' + identity_key,
            'sourceUrl': post_url, 'photoIds': identifiers, 'photoSources': sources,
            'photoSourceMode': 'browser_album', 'resolverVersion': facebook.ATTACHMENT_RESOLVER_VERSION,
            'publishedAt': published_at, 'title': '照片貼文',
            'attachments': {'state': 'complete', 'method': 'authenticated_browser_album_walk',
                            'photoCount': len(identifiers)}}


def _graph_object(identifier, fields, token, version):
    return facebook.graph_json('https://graph.facebook.com/{}/{}?{}'.format(version, identifier, urlencode({'fields': fields})), token)


def _photo_ids(collection, photos_only=False):
    items = facebook.complete_collection_data(collection)
    if not isinstance(items, list) or (not items and not photos_only):
        raise DailyError('invalid_response')
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise DailyError('invalid_response')
        if item.get('type') == 'album':
            result.extend(_photo_ids(item.get('subattachments'), photos_only=photos_only))
        elif item.get('type') == 'photo':
            identifier = item.get('target', {}).get('id') if isinstance(item.get('target'), dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r'[0-9]+', identifier):
                raise DailyError('invalid_response')
            result.append(identifier)
        elif not photos_only:
            raise DailyError('not_photos_only')
    result = list(dict.fromkeys(result))
    if photos_only and not result:
        raise DailyError('post_no_photos')
    return result


def _attachment_evidence(collection):
    evidence = [collection['completeness'].copy()]
    for attachment in collection['data']:
        if attachment.get('type') == 'album':
            evidence.extend(_attachment_evidence(attachment['subattachments']))
    return evidence


def _attachment_photo_sources(collection):
    sources = {}
    for attachment in collection['data']:
        if attachment.get('type') == 'album':
            sources.update(_attachment_photo_sources(attachment['subattachments']))
        elif attachment.get('type') == 'photo':
            media_value = attachment.get('media')
            image = media_value.get('image') if isinstance(media_value, dict) else None
            source = image.get('src') if isinstance(image, dict) else None
            if source:
                # Target identities were validated before this helper. These signed
                # native media URLs remain ephemeral and never enter audits or cache keys.
                sources[attachment['target']['id']] = facebook.checked_url(source)
    return sources


def _post_metadata_hash(item):
    """Hash stable resolved metadata; signed native image sources are ephemeral."""
    stable_metadata = {key: value for key, value in item.items() if key != 'photoSources'}
    return hashlib.sha256(json.dumps(stable_metadata, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def resolve_photo_post(photo_id, token, version, page, allow_missing_date=False, date_window=None, photos_only=False):
    """Validate parent metadata first, then resolve attachments for eligible posts."""
    bridge = _graph_object(photo_id, 'id,page_story_id', token, version)
    parent = bridge.get('page_story_id')
    if bridge.get('id') != photo_id or not isinstance(parent, str) or not re.fullmatch(r'[0-9]+_[0-9]+', parent):
        raise DailyError('invalid_response')
    retrieval = parent
    try:
        post = _graph_object(parent, POST_FIELDS, token, version)
    except facebook.FetchError as error:
        alias_page = VERIFIED_POST_PAGE_ALIASES.get(page)
        source_page_id = dict(parse_qsl(urlsplit(SOURCE_PAGE).query)).get('id')
        if (error.reason != 'permission' or page != facebook.DEFAULT_PAGE_ID or parent.split('_', 1)[0] != page
                or alias_page is None or alias_page != source_page_id):
            raise
        retrieval = alias_page + '_' + parent.split('_', 1)[1]
        post = _graph_object(retrieval, POST_FIELDS, token, version)
    author = post.get('from')
    if parent.split('_', 1)[0] != page or not isinstance(author, dict) or author.get('id') != page:
        raise DailyError('wrong_page')
    if post.get('id') != parent:
        raise DailyError('invalid_response')
    source = _source_url(post.get('permalink_url'))
    try:
        published_at = _iso(post.get('created_time'))
    except DailyError:
        if not allow_missing_date and date_window is None:
            raise
        published_at = None
    item = {'sourceId': parent, 'retrievalPostId': retrieval, 'sourceUrl': source, 'photoIds': [],
            'resolverVersion': facebook.ATTACHMENT_RESOLVER_VERSION, 'publishedAt': published_at,
            'title': post.get('message') if isinstance(post.get('message'), str) and post['message'].strip() else '照片貼文'}
    if date_window is not None:
        try:
            published = datetime.fromisoformat(published_at).astimezone(TAIPEI).date() if published_at is not None else None
        except (ValueError, TypeError, OverflowError):
            published = None
        exclusion = 'missing_date' if published is None else 'out_of_range' if not date_window[0] <= published <= date_window[1] else None
        if exclusion is not None:
            item['attachments'] = {'state': 'not_checked', 'reason': exclusion}
            return item
    complete = facebook.complete_attachments(post.get('attachments'), token, version, MAX_ATTACHMENT_PAGES)
    identifiers = _photo_ids(complete, photos_only=photos_only)
    if photo_id not in identifiers:
        raise DailyError('invalid_response')
    item['photoIds'] = identifiers
    item['photoSources'] = _attachment_photo_sources(complete)
    item['attachments'] = {'state': 'complete', 'method': 'explicit_limit_and_pagination',
                           'requestedLimit': facebook.ATTACHMENT_LIMIT, 'photoCount': len(identifiers),
                           'evidence': _attachment_evidence(complete)}
    return item


def _photo_source(identifier, token, version):
    photo = _graph_object(identifier, 'id,images', token, version)
    images = photo.get('images')
    if photo.get('id') != identifier or not isinstance(images, list):
        raise DailyError('photo_unavailable')
    available = []
    for image in images:
        if not isinstance(image, dict):
            continue
        width, height = finite_number(image.get('width')), finite_number(image.get('height'))
        if width is None or height is None or width <= 0 or height <= 0:
            continue
        source = facebook.checked_url(image.get('source'))
        available.append((width * height, width, height, source))
    if not available:
        raise DailyError('photo_unavailable')
    return max(available, key=lambda image: image[:3])[3]


def _fingerprint(paths, threshold, negative_fingerprint='', reference_threshold_margin=0.0):
    digest = hashlib.sha256(str(threshold).encode())
    digest.update(b'\0reference-threshold-margin\0')
    digest.update(str(float(reference_threshold_margin)).encode('ascii'))
    digest.update(Path(recognize.__file__).read_bytes())
    for path in paths:
        digest.update(b'\0')
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
    if negative_fingerprint:
        digest.update(b'\0negative-examples\0')
        digest.update(negative_fingerprint.encode('ascii'))
    return digest.hexdigest()


def _safe_result(result):
    state = result.get('state')
    faces = result.get('faceCount')
    score = result.get('score')
    if isinstance(faces, bool) or not isinstance(faces, int) or faces < 0 or (score is not None and finite_number(score) is None):
        raise DailyError('recognition_failure')
    if faces == 0:
        state, score = 'no_face', None
    if state not in {'candidate', 'no_match', 'no_face'} or (faces and score is None):
        raise DailyError('recognition_failure')
    safe = {'state': state, 'score': score, 'faceCount': faces}
    if result.get('method') in {'best_reference_cosine', 'reference_consensus_median', 'reference_consensus'}:
        safe['method'] = result['method']
    for key in ('referenceCount', 'usableReferenceCount', 'excludedReferenceCount',
                'negativeExampleCount', 'negativeFaceCount', 'negativeRejectedFaceCount'):
        number = result.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            safe[key] = number
    effective = finite_number(result.get('effectiveThreshold'))
    if effective is not None and 0 < effective <= 1:
        safe['effectiveThreshold'] = effective
    margin = finite_number(result.get('referenceThresholdMargin'))
    if margin is not None and 0 <= margin <= 0.1:
        safe['referenceThresholdMargin'] = margin
    if result.get('reason') == 'negative_example':
        safe['reason'] = 'negative_example'
    return safe


def _recognition_record(results, threshold, reference_count, processed_at):
    summary = {'inspectedPhotos': len(results), 'candidatePhotos': sum(item['state'] == 'candidate' for item in results),
               'noMatchPhotos': sum(item['state'] == 'no_match' for item in results), 'noFacePhotos': sum(item['state'] == 'no_face' for item in results)}
    record = {'state': 'complete', 'message': '已完成本機候選比對；候選需要家長確認，未命中不代表孩子未出現。',
              'processedAt': processed_at, 'referenceCount': reference_count, 'threshold': threshold, 'results': results, 'summary': summary}
    if results:
        record.update({key: results[0][key] for key in ('method', 'usableReferenceCount', 'excludedReferenceCount', 'effectiveThreshold', 'referenceThresholdMargin',
                                                      'negativeExampleCount', 'negativeFaceCount') if key in results[0]})
    return record


def _check_feedback_current(root, snapshot):
    if snapshot is None:
        return
    try:
        current = recognize.feedback_fingerprint(root)
    except Exception:
        raise DailyError('feedback_unavailable') from None
    if current != snapshot:
        raise DailyError('feedback_changed')


def _retained_photo_ids(manifest):
    identifiers = set()
    for post in manifest['posts']:
        if post.get('kind') != 'photos':
            continue
        for asset in post.get('assets', []):
            photo_id = source_photo_id(post, asset)
            if photo_id is not None:
                identifiers.add(photo_id)
        # Old arrays can declare retained identities without reliable positions.
        for photo_id in post.get('dailyPhotoIds', []) if isinstance(post.get('dailyPhotoIds'), list) else []:
            if isinstance(photo_id, str) and re.fullmatch(r'[0-9]+', photo_id):
                identifiers.add(photo_id)
    return identifiers


def _retained_photo_count(manifest, post_id):
    canonicals = identity_canonicals(manifest)
    visited = set()
    count = 0
    for post in manifest['posts']:
        if post.get('kind') != 'photos':
            continue
        for asset in post.get('assets', []):
            if asset.get('type') != 'image':
                continue
            canonical = canonicals.get(asset['id'])
            if canonical is None:
                # Ambiguous IDs are intentionally absent from shared groups.
                count += post['id'] == post_id
                continue
            canonical_post, canonical_asset = canonical
            if canonical_asset['id'] in visited:
                continue
            visited.add(canonical_asset['id'])
            count += canonical_post['id'] == post_id
    return count


def _commit_gallery_manifest(root, manifest, engine, backfills):
    next_profile = None
    def before_commit(previous, current):
        nonlocal next_profile
        profile = getattr(engine, 'negative_profile', None)
        if profile is not None:
            try:
                next_profile = recognize.extend_negative_profile(profile, previous, current)
            except Exception:
                raise DailyError('feedback_changed') from None
    try:
        body = feedback.commit_identity_backfills(root, manifest, backfills,
            before_commit=before_commit, expected_feedback=getattr(engine, 'feedback_snapshot', None))
    except feedback.FeedbackError as error:
        raise DailyError('feedback_changed' if error.reason == 'feedback_changed' else 'feedback_unavailable') from None
    if next_profile is not None:
        engine.negative_profile = next_profile
    engine.feedback_snapshot = body


def _gallery_record(root, item, files, results, engine, references, threshold, processed_at):
    feedback_snapshot = getattr(engine, 'negative_profile', {}).get('fingerprint')
    _check_feedback_current(root, feedback_snapshot)
    identity = 'facebook:' + item['sourceId']
    manifest = load_manifest(root)
    retained_ids = _retained_photo_ids(manifest)
    retained_snapshot = retained_ids.copy()
    groups = identity_groups(manifest)
    existing = next((post for post in manifest['posts'] if post.get('kind') == 'photos'
                     and (post.get('sourceIdentity') == identity or post.get('sourceUrl') == item['sourceUrl'])), None)
    if existing is not None:
        # Legacy full albums are retained and inspected from their own immutable originals.
        asset_photo_ids = {}
        unknown_hashes = {}
        for asset in existing.get('assets', []):
            group = groups.get(asset['id'], [(existing, asset)])
            identifiers = {source_photo_id(post, member) for post, member in group} - {None}
            photo_id = next(iter(identifiers)) if len(identifiers) == 1 else source_photo_id(existing, asset)
            asset_photo_ids[asset['id']] = photo_id
            if photo_id is None:
                path = safe_data_path(root, asset['path'], allowed={'media'})
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                unknown_hashes.setdefault(digest, []).append(asset['id'])
        incoming = list(zip(item['photoIds'], files, results))
        backfills = {}
        matched_groups = set()
        legacy_ids = {photo_id for photo_id in existing.get('dailyPhotoIds', [])
                      if isinstance(photo_id, str) and re.fullmatch(r'[0-9]+', photo_id)} if isinstance(existing.get('dailyPhotoIds'), list) else set()
        local_photo_ids = set(asset_photo_ids.values()) - {None}
        # Match declared legacy identities first; a new ID must not consume their originals.
        for photo_id, path, _ in sorted(incoming, key=lambda item: item[0] not in legacy_ids):
            if photo_id in local_photo_ids:
                continue
            matches = unknown_hashes.get(hashlib.sha256(path.read_bytes()).hexdigest(), [])
            while matches and matches[0] in matched_groups:
                matches.pop(0)
            if matches:
                asset_id = matches.pop(0)
                group = groups.get(asset_id, [(existing, next(asset for asset in existing['assets'] if asset['id'] == asset_id))])
                for _, member in group:
                    matched_groups.add(member['id'])
                    if member['id'] in asset_photo_ids:
                        backfills[member['id']] = photo_id
                        asset_photo_ids[member['id']] = photo_id
                retained_ids.add(photo_id)
                local_photo_ids.add(photo_id)
        records = []
        for asset in existing.get('assets', []):
            if asset.get('type') != 'image':
                raise DailyError('import_failure')
            path = safe_data_path(root, asset.get('path'), allowed={'media'})
            records.append({'assetId': asset['id'], **_safe_result(recognize.analyze_image(
                path, engine, references, threshold, photo_id=asset_photo_ids[asset['id']]))})
        known_ids = existing.get('dailyPhotoIds')
        if not isinstance(known_ids, list) or len(known_ids) != len(existing.get('assets', [])):
            known_ids = None
        additions = [(photo_id, path, result) for photo_id, path, result in incoming
                     if result['state'] == 'candidate' and photo_id not in retained_ids]
        if additions:
            # Use the existing validated import path in a private scratch root, then append its files.
            # The source post and all original asset IDs stay intact.
            with tempfile.TemporaryDirectory(prefix='.promotion-', dir=str(_private_directory(root, 'downloads'))) as temporary:
                scratch = Path(temporary)
                added = media.import_media(scratch, 'photos', item['sourceUrl'], [path for _, path, _ in additions],
                                           title=item['title'], published_at=item['publishedAt'])
                source = safe_data_path(scratch, 'media/' + added['id'])
                destination = safe_data_path(root, 'media/' + added['id'])
                installed = False
                try:
                    with manifest_lock(root):
                        _check_feedback_current(root, feedback_snapshot)
                        manifest = load_manifest(root)
                        current = next((post for post in manifest['posts'] if post.get('id') == existing['id']), None)
                        if (current is None or current.get('assets') != existing.get('assets')
                                or _retained_photo_ids(manifest) != retained_snapshot):
                            raise DailyError('import_failure')
                        os.rename(source, destination)
                        installed = True
                        for asset, (photo_id, _, _) in zip(added['assets'], additions):
                            asset['sourcePhotoId'] = photo_id
                        for asset in current['assets']:
                            if asset['id'] in backfills:
                                asset['sourcePhotoId'] = backfills[asset['id']]
                        current['assets'].extend(added['assets'])
                        records.extend({'assetId': asset['id'], **result} for asset, (_, _, result) in zip(added['assets'], additions))
                        current['recognition'] = _recognition_record(records, threshold, len(references), processed_at)
                        current['clips'] = []
                        if known_ids is not None:
                            current['dailyPhotoIds'] = known_ids + [photo_id for photo_id, _, _ in additions]
                        _commit_gallery_manifest(root, manifest, engine, backfills)
                except Exception:
                    if installed:
                        shutil.rmtree(destination, ignore_errors=True)
                    raise
            return existing['id']
        with manifest_lock(root):
            _check_feedback_current(root, feedback_snapshot)
            manifest = load_manifest(root)
            current = next((post for post in manifest['posts'] if post.get('id') == existing['id']), None)
            if (current is None or current.get('assets') != existing.get('assets')
                    or _retained_photo_ids(manifest) != retained_snapshot):
                raise DailyError('import_failure')
            for asset in current['assets']:
                if asset['id'] in backfills:
                    asset['sourcePhotoId'] = backfills[asset['id']]
            current['recognition'] = _recognition_record(records, threshold, len(references), processed_at)
            current['clips'] = []
            _commit_gallery_manifest(root, manifest, engine, backfills)
        return existing['id']
    candidates = [(photo_id, path, result) for photo_id, path, result in zip(item['photoIds'], files, results)
                  if result['state'] == 'candidate' and photo_id not in retained_ids]
    if not candidates:
        return None
    def before_commit():
        _check_feedback_current(root, feedback_snapshot)
        if _retained_photo_ids(load_manifest(root)) != retained_snapshot:
            raise DailyError('import_failure')
    post = media.import_media(root, 'photos', item['sourceUrl'], [path for _, path, _ in candidates], title=item['title'],
                              published_at=item['publishedAt'], source_id=identity,
                              before_commit=before_commit)
    records = [{'assetId': asset['id'], **result} for asset, (_, _, result) in zip(post['assets'], candidates)]
    with manifest_lock(root):
        _check_feedback_current(root, feedback_snapshot)
        manifest = load_manifest(root)
        current = next((value for value in manifest['posts'] if value.get('id') == post['id']), None)
        if current is None or current.get('assets') != post.get('assets'):
            raise DailyError('import_failure')
        current['recognition'] = _recognition_record(records, threshold, len(references), processed_at)
        # Photo IDs are public stable identities, allowing future additions to avoid duplicating files.
        current['dailyPhotoIds'] = [photo_id for photo_id, _, _ in candidates]
        for asset, photo_id in zip(current['assets'], current['dailyPhotoIds']):
            asset['sourcePhotoId'] = photo_id
        save_manifest(root, manifest)
    return post['id']


def _report(now, simulate):
    clock = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise DailyError('configuration')
    return {'version': 1, 'runId': 'daily-' + uuid.uuid4().hex, 'state': 'blocked', 'simulated': bool(simulate),
            'startedAt': clock.isoformat(), 'schedule': SCHEDULE.copy(),
            'discovery': {'mode': 'replay' if simulate else 'live', 'capturedAt': None},
            'counts': {key: 0 for key in ('discoveredPosts', 'processedPosts', 'cachedPosts', 'skippedPosts', 'duplicatePosts', 'failedPosts',
                                        'unprocessedObservations',
                                        'outOfRangePosts', 'missingDatePosts',
                                        'inspectedPhotos', 'candidatePhotos', 'noMatchPhotos', 'noFacePhotos', 'skippedVideos', 'likedPhotos')},
            'posts': [], 'failures': []}


def _gallery_cache_fingerprint(root, item):
    """Bind cached counts to the retained gallery and its validated original bytes."""
    identity = 'facebook:' + item['sourceId']
    manifest = load_manifest(root)
    gallery = next((post for post in manifest['posts'] if post.get('kind') == 'photos'
                    and (post.get('sourceIdentity') == identity or post.get('sourceUrl') == item['sourceUrl'])), None)
    if gallery is None:
        retained = sorted(set(item.get('photoIds', [])) & _retained_photo_ids(manifest))
        return 'retained:' + hashlib.sha256(json.dumps(retained).encode()).hexdigest() if retained else None
    if (gallery.get('recognition', {}).get('state') != 'complete'
            or not media.originals_available(root, gallery, 'photos', len(gallery.get('assets', [])))):
        return False
    try:
        digest = hashlib.sha256(json.dumps(gallery, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())
        digest.update(b'\0logical-retained-count\0')
        digest.update(str(_retained_photo_count(manifest, gallery['id'])).encode('ascii'))
        for asset in gallery['assets']:
            digest.update(b'\0')
            path = safe_data_path(root, asset['path'], allowed={'media'})
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
        return digest.hexdigest()
    except (MediaError, OSError, ValueError):
        return False


def _conditional_reaction(root, item, audit, profile, auto_like, simulate, report, state, feedback_snapshot=None):
    """Like individual candidate photos; local exclusions never undo remote likes."""
    _check_feedback_current(root, feedback_snapshot)
    candidates = list(dict.fromkeys(result['photoId'] for result in audit.get('results', [])
                                   if isinstance(result, dict) and result.get('state') == 'candidate'
                                   and result.get('photoId') in item['photoIds']))
    if audit.get('counts', {}).get('candidatePhotos') and not candidates:
        raise DailyError('local_storage')
    audit['reactionTarget'] = 'photo'
    audit['photoReactions'] = []
    audit['reaction'] = 'suppressed_simulation' if simulate else 'not_requested'
    for photo_id in candidates:
        _check_feedback_current(root, feedback_snapshot)
        previous = state['photoReactions'].get(photo_id)
        if previous is not None:
            reaction = previous['reaction']
        elif simulate or not auto_like:
            reaction = 'suppressed_simulation' if simulate else 'not_requested'
        else:
            try:
                result = _browser(root, 'react-photo', profile, photo_id)
                if (result.get('state') != 'complete' or result.get('photoId') != photo_id
                        or result.get('reaction') not in {'liked', 'already_liked'}):
                    raise DailyError('reaction_failure')
                reaction = result['reaction']
            except Exception:
                reaction = 'failed'
                report['failures'].append({'sourceId': item['sourceId'], 'photoId': photo_id,
                                          'reason': 'reaction_failure', 'message': MESSAGES['reaction_failure']})
            else:
                # Persist every verified photo before advancing. Rechecks and
                # duplicate appearances in another post share this Photo ID.
                state['photoReactions'][photo_id] = {'reaction': reaction, 'checkedAt': report['startedAt']}
                try:
                    _atomic_json(root, 'daily-state.json', state)
                except Exception:
                    raise DailyError('local_storage') from None
                report['counts']['likedPhotos'] += reaction == 'liked'
        audit['photoReactions'].append({'photoId': photo_id, 'reaction': reaction})
    reactions = {entry['reaction'] for entry in audit['photoReactions']}
    if 'failed' in reactions:
        audit['reaction'] = 'failed'
    elif not simulate and auto_like and reactions:
        audit['reaction'] = 'liked' if 'liked' in reactions else 'already_liked'
    # A parent may exclude the final photo while the browser is working.
    # Preserve the verified remote like, but stop before claiming stale results.
    _check_feedback_current(root, feedback_snapshot)


def _persist_report(root, report):
    report['finishedAt'] = datetime.now(timezone.utc).isoformat()
    report['lastRunAt'] = report['finishedAt']
    report['discoveryMode'] = report['discovery']['mode']
    _atomic_json(root, 'runs/' + report['runId'] + '.json', report)
    with manifest_lock(root):
        manifest = load_manifest(root)
        manifest['jobStatus'] = report
        save_manifest(root, manifest)
    return report


def run_daily(root=ROOT, discovery_file=None, simulate=False, threshold=0.5, env_file=None, now=None,
              start_date=None, end_date=None, max_scrolls=None, force_recheck=False, progress_id=None, post_url=None):
    """Run one serialized photo batch; replay still uses real API, downloads and matching."""
    root = Path(root).resolve()
    report = _report(now, simulate)
    if progress_id is not None:
        try:
            if str(uuid.UUID(progress_id)) != progress_id:
                progress_id = None
        except (AttributeError, TypeError, ValueError):
            progress_id = None
    _manual_progress(root, progress_id, 'preparing')
    if discovery_file is not None:
        report['discovery']['mode'] = 'replay'
    try:
        window = _date_window(start_date, end_date, datetime.fromisoformat(report['startedAt']))
        if window is not None:
            report['range'] = {'startDate': window[0].isoformat(), 'endDate': window[1].isoformat(), 'timeZone': SCHEDULE['timeZone']}
        if max_scrolls is not None and (isinstance(max_scrolls, bool) or not isinstance(max_scrolls, int) or not 1 <= max_scrolls <= 100):
            raise DailyError('configuration')
        with _batch_lock(root):
            try:
                if finite_number(threshold) is None or not 0 < threshold <= 1:
                    raise DailyError('configuration')
                try:
                    token, version, page, profile, auto_like, configured_max_scrolls = _configuration(root, env_file)
                    reference_threshold_margin = recognize.load_reference_threshold_margin(root)
                    if post_url is not None:
                        if discovery_file is not None or simulate or window is not None:
                            raise DailyError('invalid_post_url')
                        post_url = _post_url(post_url, page)
                except DailyError:
                    raise
                except (MediaError, OSError, ValueError, TypeError):
                    raise DailyError('configuration') from None
                try:
                    paths, models = recognize.check_inputs(root)
                    engine = recognize.FaceEngine(models)
                    engine.reference_threshold_margin = reference_threshold_margin
                    references = []
                    for path in paths:
                        faces = engine.describe_file(path)
                        if len(faces) != 1:
                            raise DailyError('recognition_prerequisites')
                        references.append(faces[0])
                    policy = recognize.prepare_reference_profile(references, threshold, reference_threshold_margin)
                    try:
                        with manifest_lock(root):
                            engine.feedback_snapshot = feedback.commit_identity_backfills(root, load_manifest(root))
                            engine.negative_profile = recognize.load_negative_profile(root, engine)
                    except Exception:
                        raise DailyError('feedback_unavailable') from None
                    report['recognitionPolicy'] = {key: policy[key] for key in ('method', 'referenceCount', 'usableReferenceCount',
                                                                               'excludedReferenceCount', 'effectiveThreshold')}
                    if 'referenceThresholdMargin' in policy:
                        report['recognitionPolicy']['referenceThresholdMargin'] = policy['referenceThresholdMargin']
                    if engine.negative_profile['exampleCount']:
                        report['recognitionPolicy'].update(negativeExampleCount=engine.negative_profile['exampleCount'],
                                                          negativeFaceCount=engine.negative_profile['faceCount'])
                    fingerprint = _fingerprint(paths + models, threshold, engine.negative_profile['fingerprint'], reference_threshold_margin)
                except DailyError:
                    raise
                except Exception:
                    raise DailyError('recognition_prerequisites') from None
                effective_scroll_limit = configured_max_scrolls if max_scrolls is None else max_scrolls
                if post_url is not None:
                    _manual_progress(root, progress_id, 'opening_post', completedPosts=0, totalPosts=1)
                else:
                    _manual_progress(root, progress_id, 'scanning', scrolls=0, scrollLimit=effective_scroll_limit,
                                     discoveredPosts=0)
                if progress_id is None:
                    discovery, observations, skipped = _discovery(root, discovery_file, simulate, profile,
                                                                  effective_scroll_limit, post_url=post_url)
                else:
                    discovery, observations, skipped = _discovery(root, discovery_file, simulate, profile,
                                                                  effective_scroll_limit, progress_id, post_url)
                report['discovery'] = discovery
                report['counts']['skippedVideos'] = skipped
                report['counts']['discoveredPosts'] = len(observations)
                _manual_progress(root, progress_id, 'resolving', completedPosts=0,
                                 totalPosts=len(observations), currentPost=0)
                if discovery['state'] == 'blocked':
                    raise DailyError(discovery['reason'] or 'browser_unavailable')
                state_path = safe_data_path(root, 'daily-state.json')
                try:
                    state = _read_json(state_path) if state_path.exists() else {'version': 1, 'posts': {}}
                    if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('posts'), dict):
                        raise ValueError()
                    state.setdefault('photoReactions', {})
                    if (not isinstance(state['photoReactions'], dict)
                            or any(not isinstance(photo_id, str) or not re.fullmatch(r'[0-9]+', photo_id)
                                   or not isinstance(entry, dict) or entry.get('reaction') not in {'liked', 'already_liked'}
                                   or not isinstance(entry.get('checkedAt'), str)
                                   for photo_id, entry in state['photoReactions'].items())):
                        raise ValueError()
                except (OSError, ValueError, UnicodeError):
                    raise DailyError('local_storage') from None
                seen = set()
                downloads = _private_directory(root, 'downloads')
                completed_posts = 0
                for observation_index, observation in enumerate(observations):
                    parent = None
                    phase = 'remote_failure'
                    _manual_progress(root, progress_id, 'resolving', completedPosts=completed_posts,
                                     totalPosts=len(observations), currentPost=observation_index + 1)
                    try:
                        _check_feedback_current(root, engine.negative_profile['fingerprint'])
                        try:
                            item = resolve_photo_post(observation['photoId'], token, version, page,
                                                      allow_missing_date=window is not None, date_window=window,
                                                      photos_only=post_url is not None)
                        except facebook.FetchError as error:
                            if post_url is None or error.reason != 'permission':
                                raise
                            _manual_progress(root, progress_id, 'collecting', completedPosts=completed_posts,
                                             totalPosts=len(observations), currentPost=observation_index + 1,
                                             currentPhoto=0)
                            item = _browser_post_photo_item(root, profile, post_url, page,
                                                            observation['photoId'], progress_id)
                        parent = item['sourceId']
                        if parent in seen:
                            report['counts']['duplicatePosts'] += 1
                            continue
                        seen.add(parent)
                        if window is not None:
                            try:
                                published = datetime.fromisoformat(item['publishedAt']).astimezone(TAIPEI).date()
                            except (ValueError, TypeError, OverflowError):
                                published = None
                            exclusion = 'missing_date' if published is None else 'out_of_range' if not window[0] <= published <= window[1] else None
                            if exclusion is not None:
                                report['counts']['missingDatePosts' if exclusion == 'missing_date' else 'outOfRangePosts'] += 1
                                report['counts']['skippedPosts'] += 1
                                report['posts'].append({'sourceId': parent, 'retrievalPostId': item['retrievalPostId'],
                                                        'state': 'skipped', 'reason': exclusion, 'reaction': 'not_requested'})
                                continue
                        # Signed attachment image URLs can rotate independently of a
                        # post's contents and must not invalidate or enter cache metadata.
                        metadata = _post_metadata_hash(item)
                        cached = state['posts'].get(parent)
                        if (not simulate and not force_recheck and isinstance(cached, dict)
                                and cached.get('resolverVersion') == facebook.ATTACHMENT_RESOLVER_VERSION
                                and cached.get('metadataHash') == metadata and cached.get('referenceFingerprint') == fingerprint
                                and 'galleryFingerprint' in cached and cached['galleryFingerprint'] is not False
                                and cached['galleryFingerprint'] == _gallery_cache_fingerprint(root, item)):
                            _check_feedback_current(root, engine.negative_profile['fingerprint'])
                            cached_audit = cached.get('audit', {})
                            _conditional_reaction(root, item, cached_audit, profile, auto_like, simulate, report, state,
                                                  engine.negative_profile['fingerprint'])
                            report['counts']['skippedPosts'] += 1
                            report['counts']['cachedPosts'] += 1
                            report['posts'].append({**cached_audit, 'state': 'skipped', 'reason': 'already_processed',
                                                    'checkMode': 'cached', 'newInspectedPhotoCount': 0})
                            _atomic_json(root, 'daily-state.json', state)
                            continue
                        phase = 'download_failure'
                        _manual_progress(root, progress_id, 'downloading', completedPosts=completed_posts,
                                         totalPosts=len(observations), currentPost=observation_index + 1,
                                         currentPhoto=0, currentPhotoTotal=len(item['photoIds']))
                        with tempfile.TemporaryDirectory(prefix='.daily-', dir=str(downloads)) as temporary:
                            files = []
                            photo_lookups = 0
                            for index, photo_id in enumerate(item['photoIds'], 1):
                                source = item['photoSources'].get(photo_id)
                                if source is None:
                                    photo_lookups += 1
                                    source = _photo_source(photo_id, token, version)
                                path = facebook.download_native(source, Path(temporary), index, 'photos')
                                os.chmod(path, 0o600)
                                files.append(path)
                                _manual_progress(root, progress_id, 'downloading', completedPosts=completed_posts,
                                                 totalPosts=len(observations), currentPost=observation_index + 1,
                                                 currentPhoto=index, currentPhotoTotal=len(item['photoIds']))
                            phase = 'recognition_failure'
                            results = []
                            for index, (photo_id, path) in enumerate(zip(item['photoIds'], files), 1):
                                results.append(_safe_result(recognize.analyze_image(path, engine, references, threshold,
                                                                                   photo_id=photo_id)))
                                _manual_progress(root, progress_id, 'recognizing', completedPosts=completed_posts,
                                                 totalPosts=len(observations), currentPost=observation_index + 1,
                                                 currentPhoto=index, currentPhotoTotal=len(files))
                            _check_feedback_current(root, engine.negative_profile['fingerprint'])
                            phase = 'import_failure'
                            previous_feedback_fingerprint = engine.negative_profile['fingerprint']
                            gallery_id = _gallery_record(root, item, files, results, engine, references, threshold, report['startedAt'])
                            if engine.negative_profile['fingerprint'] != previous_feedback_fingerprint:
                                _check_feedback_current(root, engine.negative_profile['fingerprint'])
                                results = []
                                for index, (photo_id, path) in enumerate(zip(item['photoIds'], files), 1):
                                    results.append(_safe_result(recognize.analyze_image(path, engine, references,
                                                                                       threshold, photo_id=photo_id)))
                                    _manual_progress(root, progress_id, 'recognizing', completedPosts=completed_posts,
                                                     totalPosts=len(observations), currentPost=observation_index + 1,
                                                     currentPhoto=index, currentPhotoTotal=len(files))
                                _check_feedback_current(root, engine.negative_profile['fingerprint'])
                                fingerprint = _fingerprint(paths + models, threshold, engine.negative_profile['fingerprint'], reference_threshold_margin)
                                gallery_id = _gallery_record(root, item, files, results, engine, references, threshold, report['startedAt'])
                        audit = {'sourceId': parent, 'retrievalPostId': item['retrievalPostId'], 'state': 'complete',
                                 'publishedAt': item['publishedAt'], 'sourceUrl': item['sourceUrl'], 'title': item['title'],
                                 'photoCount': len(item['photoIds']), 'inspectedPhotoCount': len(results),
                                 'candidatePhotoCount': sum(result['state'] == 'candidate' for result in results),
                                 'retainedPhotoCount': 0, 'checkedAt': report['startedAt'],
                                 'attachments': item['attachments'], 'resolverVersion': item['resolverVersion'],
                                 'checkMode': 'fresh', 'newInspectedPhotoCount': len(results),
                                 'photoSourceMode': item.get('photoSourceMode') or ('attachment_media' if photo_lookups == 0 else 'largest_photo_image' if photo_lookups == len(results) else 'mixed'),
                                 'photoSourceQuality': 'browser_viewer' if item.get('photoSourceMode') == 'browser_album' else 'attachment_provided' if photo_lookups == 0 else 'largest_available' if photo_lookups == len(results) else 'mixed',
                                 'photoObjectLookups': photo_lookups,
                                 'results': [{'photoId': photo_id, **result} for photo_id, result in zip(item['photoIds'], results)],
                                 'counts': _recognition_record(results, threshold, len(references), report['startedAt'])['summary'],
                                 'reaction': 'suppressed_simulation' if simulate else 'not_requested'}
                        if gallery_id:
                            audit['galleryPostId'] = gallery_id
                            manifest = load_manifest(root)
                            gallery = next((post for post in manifest['posts'] if post.get('id') == gallery_id), None)
                            if gallery is None:
                                raise DailyError('import_failure')
                            audit['retainedPhotoCount'] = _retained_photo_count(manifest, gallery_id)
                        gallery_fingerprint = _gallery_cache_fingerprint(root, item)
                        if gallery_fingerprint is False:
                            raise DailyError('import_failure')
                        _conditional_reaction(root, item, audit, profile, auto_like, simulate, report, state,
                                              engine.negative_profile['fingerprint'])
                        for key, value in audit['counts'].items():
                            report['counts'][key] += value
                        report['counts']['processedPosts'] += 1
                        report['posts'].append(audit)
                        state['posts'][parent] = {'metadataHash': metadata, 'referenceFingerprint': fingerprint,
                                                 'resolverVersion': facebook.ATTACHMENT_RESOLVER_VERSION,
                                                 'galleryFingerprint': gallery_fingerprint, 'audit': audit}
                        _atomic_json(root, 'daily-state.json', state)
                        _manual_progress(root, progress_id, 'saving', completedPosts=completed_posts,
                                         totalPosts=len(observations), currentPost=observation_index + 1)
                    except Exception as error:
                        reason = _reason(error, locals().get('phase', 'remote_failure'))
                        report['counts']['failedPosts'] += 1
                        failure = {'reason': reason, 'message': MESSAGES[reason]}
                        if parent is not None:
                            failure['sourceId'] = parent
                        report['failures'].append(failure)
                        if reason in {'rate_limit', 'feedback_changed', 'feedback_unavailable'}:
                            report['counts']['unprocessedObservations'] = len(observations) - observation_index - 1
                            break
                    finally:
                        completed_posts = observation_index + 1
                        _manual_progress(root, progress_id, 'resolving', completedPosts=completed_posts,
                                         totalPosts=len(observations), currentPost=0)
                if report['failures']:
                    report['state'] = 'partial' if report['counts']['processedPosts'] or report['counts']['skippedPosts'] else 'blocked'
                    terminal = report['failures'][-1]
                    failure = terminal if terminal['reason'] in {'rate_limit', 'feedback_changed', 'feedback_unavailable'} else report['failures'][0]
                    report['reason'] = failure['reason']
                    report['message'] = failure['message']
                elif discovery['state'] == 'partial':
                    report.update(state='partial', reason=discovery['reason'] or 'scan_limit', message=MESSAGES[discovery['reason'] or 'scan_limit'])
                elif report['counts']['missingDatePosts']:
                    report.update(state='partial', reason='missing_date', message=MESSAGES['missing_date'])
                else:
                    report.update(state='complete', reason='photos_checked', message='每日照片檢查完成；候選需要家長確認。')
            except Exception as error:
                reason = _reason(error, 'local_storage')
                report.update(state='blocked', reason=reason, message=MESSAGES[reason])
            _manual_progress(root, progress_id, 'finalizing', completedPosts=report['counts']['processedPosts'] +
                             report['counts']['skippedPosts'] + report['counts']['failedPosts'],
                             totalPosts=report['counts']['discoveredPosts'])
            return _persist_report(root, report)
    except Exception as error:
        reason = _reason(error, 'local_storage')
        report.update(state='blocked', reason=reason, message=MESSAGES[reason], finishedAt=datetime.now(timezone.utc).isoformat())
        report['lastRunAt'] = report['finishedAt']
        report['discoveryMode'] = report['discovery']['mode']
        try:
            # Rejected triggers have their own audit, without overwriting an active job status.
            # If manifest persistence failed, replace any earlier archive with this honest result.
            _atomic_json(root, 'runs/' + report['runId'] + '.json', report)
        except Exception:
            pass  # The sanitized local-storage result is still returned when disk writes are unavailable.
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='每日照片：私有下載、本機比對，只加入候選照片')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--simulate', action='store_true', help='重播發現資料，仍以實際 API 下載辨識，不按讚')
    parser.add_argument('--discovery-file', type=Path)
    parser.add_argument('--post-url', help='直接處理指定 Facebook 貼文，不掃描動態列表')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--threshold', type=float, default=0.5)
    parser.add_argument('--force-recheck', action='store_true', help='重新下載並比對範圍內完整照片貼文，略過既有檢查快取')
    parser.add_argument('--start-date', help='台北開始日期 YYYY-MM-DD，須與結束日期一起指定')
    parser.add_argument('--end-date', help='台北結束日期 YYYY-MM-DD，包含當天')
    def progress_identifier(value):
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError()
            return value
        except (AttributeError, TypeError, ValueError):
            raise argparse.ArgumentTypeError('進度識別碼無效') from None
    parser.add_argument('--progress-id', type=progress_identifier, help=argparse.SUPPRESS)
    def scroll_limit(value):
        try:
            result = int(value)
            if not 1 <= result <= 100:
                raise ValueError()
            return result
        except ValueError:
            raise argparse.ArgumentTypeError('掃描上限須為 1 到 100 的整數') from None
    parser.add_argument('--max-scrolls', type=scroll_limit, help='本次瀏覽器掃描上限，1 到 100')
    arguments = parser.parse_args(argv)
    report = run_daily(arguments.root, arguments.discovery_file, arguments.simulate, arguments.threshold, arguments.env_file,
                       start_date=arguments.start_date, end_date=arguments.end_date, max_scrolls=arguments.max_scrolls,
                       force_recheck=arguments.force_recheck, progress_id=arguments.progress_id, post_url=arguments.post_url)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report['state'] == 'complete' else 2 if report['state'] == 'partial' else 1


if __name__ == '__main__':
    sys.exit(main())
