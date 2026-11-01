#!/usr/bin/env python3
"""Fetch two native-media posts using explicitly configured Facebook Graph API access."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from common import (MediaError, SECRET_QUERY_KEYS, data_directory, load_manifest, manifest_lock,
                    save_manifest)
from media import import_media, originals_available, validate_source_url

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAGE_ID = "864287026968664"
ATTACHMENT_LIMIT = 100
ATTACHMENT_RESOLVER_VERSION = 2
FIELDS = "id,message,created_time,permalink_url,attachments.limit(100){type,url,target,media,subattachments.limit(100){type,url,target,media}}"
ENV_KEYS = {"FB_ACCESS_TOKEN", "FB_GRAPH_VERSION", "FB_PAGE_ID", "FB_APP_ID", "FB_APP_SECRET"}


class FetchError(MediaError):
    """Only a safe local message, never a remote URL or response body."""
    def __init__(self, message, state="error", reason="remote_failure"):
        super().__init__(message)
        self.state = state
        self.reason = reason


def checked_url(url, graph=False):
    """Allow only HTTPS Graph or Facebook CDN endpoints, without URL credentials."""
    try:
        if not isinstance(url, str) or not url or "\\" in url or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError()
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.port is not None:
            raise ValueError()
        if graph:
            allowed = host == "graph.facebook.com"
        else:
            allowed = any(host == domain or host.endswith("." + domain) for domain in ("facebook.com", "fbcdn.net", "fbsbx.com"))
        if not allowed or "\\" in unquote(parts.path) or any(p in {".", ".."} for p in unquote(parts.path).split("/")):
            raise ValueError()
    except (ValueError, TypeError):
        raise FetchError("Facebook 回傳不允許的連線網址", reason="unsafe_url") from None
    query = parts.query
    if graph:
        query = urlencode([(key, value) for key, value in parse_qsl(query, keep_blank_values=True)
                           if key.lower() not in SECRET_QUERY_KEYS])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        graph = urlsplit(request.full_url).hostname == "graph.facebook.com"
        destination = checked_url(new_url, graph=graph)
        redirected = super().redirect_request(request, response, code, message, headers, destination)
        if redirected is not None and not graph:
            redirected.remove_header("Authorization")
        return redirected


# One replaceable network boundary; redirects still go through the allowlist above.
urlopen = build_opener(SafeRedirectHandler()).open


def graph_failure(error):
    code = error.get("code") if isinstance(error, dict) else None
    if code in {4, 17, 32, 613}:
        return FetchError("Facebook API 暫時達到讀取上限；已停止後續請求，請稍後重試", "blocked", "rate_limit")
    if code in {10, 200}:
        return FetchError("Facebook 拒絕讀取此專頁；請確認已核准的權限或適用的 Page Public Content Access", "blocked", "permission")
    if code in {102, 190}:
        return FetchError("Facebook 存取權杖無效或已過期；請在本機更新 FB_ACCESS_TOKEN", "blocked", "credentials")
    return FetchError("Facebook Graph API 讀取失敗；請確認應用程式設定與可用 API 版本")


def graph_json(url, token):
    request = Request(checked_url(url, graph=True), headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
    except HTTPError as error:
        try:
            body = json.loads(error.read(8 * 1024 * 1024).decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            body = {}
        finally:
            error.close()
        if error.code == 429:
            raise FetchError("Facebook API 暫時達到讀取上限；已停止後續請求，請稍後重試", "blocked", "rate_limit") from None
        if isinstance(body, dict) and "error" in body:
            raise graph_failure(body["error"]) from None
        if error.code in {401, 403}:
            raise FetchError("Facebook 拒絕存取；請確認本機權杖與已核准的專頁權限", "blocked", "permission") from None
        raise FetchError("Facebook Graph API HTTP 讀取失敗") from None
    except (URLError, OSError, ValueError):
        raise FetchError("無法連線 Facebook Graph API；請確認本機網路") from None
    if len(payload) > 8 * 1024 * 1024:
        raise FetchError("Facebook 回應超過本機處理上限", reason="invalid_response")
    try:
        body = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise FetchError("Facebook 回傳無效的 JSON 資料", reason="invalid_response") from None
    if not isinstance(body, dict):
        raise FetchError("Facebook 回傳無效的資料格式", reason="invalid_response")
    if "error" in body:
        raise graph_failure(body["error"])
    return body


def complete_attachments(collection, token, version, max_pages, depth=0):
    """Resolve each attachment edge fully before it can be classified or imported."""
    if depth > 100:
        raise FetchError("Facebook 相簿巢狀層數超過本機上限；未匯入不完整貼文", reason="pagination_limit")
    attachments, visited = [], set()
    total_count = None
    body = collection
    for index in range(max_pages):
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise FetchError("Facebook 附件回應缺少有效 data 清單", reason="invalid_response")
        summary = body.get("summary")
        if summary is not None:
            if not isinstance(summary, dict):
                raise FetchError("Facebook 附件總數格式無效", reason="invalid_response")
            if "total_count" in summary:
                reported = summary["total_count"]
                if isinstance(reported, bool) or not isinstance(reported, int) or reported < 0 or (total_count is not None and total_count != reported):
                    raise FetchError("Facebook 附件總數格式無效或分頁不一致", reason="invalid_response")
                total_count = reported
        attachments.extend(body["data"])
        if len(attachments) > 10000:
            raise FetchError("Facebook 附件數量超過本機上限；未匯入不完整貼文", reason="pagination_limit")
        paging = body.get("paging")
        if paging is not None and not isinstance(paging, dict):
            raise FetchError("Facebook 附件分頁格式無效", reason="invalid_response")
        next_url = paging.get("next") if paging else None
        if next_url is None or next_url == "":
            if len(body["data"]) >= ATTACHMENT_LIMIT:
                raise FetchError("Facebook 附件達查詢上限且未提供下一頁；未匯入不完整貼文", reason="pagination_limit")
            break
        url = checked_url(next_url, graph=True)
        if not urlsplit(url).path.startswith("/" + version + "/"):
            raise FetchError("Facebook 分頁未使用指定 API 版本", reason="unsafe_url")
        if url in visited:
            raise FetchError("Facebook 附件分頁循環；未匯入不完整貼文", reason="pagination_cycle")
        if index + 1 >= max_pages:
            raise FetchError("Facebook 附件分頁超過 max-pages；請提高上限後重試", reason="pagination_limit")
        visited.add(url)
        body = graph_json(url, token)
    if total_count is not None and len(attachments) != total_count:
        reason = "pagination_limit" if len(attachments) < total_count else "invalid_response"
        raise FetchError("Facebook 附件讀取數量與回傳總數不符；未匯入不完整貼文", reason=reason)
    identifiers = set()
    for attachment in attachments:
        target = attachment.get("target") if isinstance(attachment, dict) else None
        identifier = target.get("id") if isinstance(target, dict) else None
        if isinstance(identifier, str) and identifier:
            if identifier in identifiers:
                raise FetchError("Facebook 附件包含重複識別碼；未匯入無法驗證完整性的貼文", reason="invalid_response")
            identifiers.add(identifier)
    result = []
    for attachment in attachments:
        if isinstance(attachment, dict) and attachment.get("type") == "album" and isinstance(attachment.get("subattachments"), dict):
            attachment = {**attachment, "subattachments": complete_attachments(
                attachment["subattachments"], token, version, max_pages, depth + 1)}
        result.append(attachment)
    proof = {"state": "complete", "method": "explicit_limit_and_pagination", "requestedLimit": ATTACHMENT_LIMIT,
             "depth": depth, "pageCount": index + 1, "itemCount": len(result), "terminalPageCount": len(body["data"])}
    if total_count is not None:
        proof["totalCount"] = total_count
    return {"data": result, "completeness": proof}


def complete_collection_data(collection):
    """Unresolved paging must never masquerade as a complete attachment list."""
    if not isinstance(collection, dict):
        return None
    paging = collection.get("paging")
    if paging is not None and (not isinstance(paging, dict) or paging.get("next")):
        return None
    proof = collection.get("completeness")
    if proof is not None and (not isinstance(proof, dict) or proof.get("state") != "complete"):
        return None
    summary = collection.get("summary")
    if summary is not None and (not isinstance(summary, dict) or ("total_count" in summary and
            (isinstance(summary["total_count"], bool) or not isinstance(summary["total_count"], int)
             or not isinstance(collection.get("data"), list) or summary["total_count"] != len(collection["data"])))):
        return None
    return collection.get("data")


def attachment_leaves(attachments):
    """Flatten only native albums; external link wrappers cannot become photos."""
    if not isinstance(attachments, list) or not attachments:
        return None
    result = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            return None
        kind = attachment.get("type")
        if kind == "album":
            nested = attachment.get("subattachments")
            children = attachment_leaves(complete_collection_data(nested))
            if children is None:
                return None
            result.extend(children)
        elif kind == "photo":
            media = attachment.get("media")
            image = media.get("image") if isinstance(media, dict) else None
            url = image.get("src") if isinstance(image, dict) else None
            if not isinstance(url, str) or not url:
                return None
            result.append(("photos", url))
        elif kind in {"video", "video_inline", "video_autoplay"}:
            target = attachment.get("target")
            identifier = target.get("id") if isinstance(target, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r"[0-9]+", identifier):
                return None
            result.append(("video", identifier))
        else:
            return None
    return result


def select_posts(posts, since):
    """Choose the newest supported photos-only and single-video-only posts."""
    selected = {}
    dates = {}
    for post in posts:
        if not isinstance(post, dict):
            continue
        try:
            date = post["created_time"].replace("Z", "+00:00")
            # Graph uses +0000; Python 3.9 requires the offset colon.
            date = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", date)
            published = datetime.fromisoformat(date)
            if published.tzinfo is None or published.timestamp() < since:
                continue
            parts = urlsplit(post["permalink_url"])
            source_url = validate_source_url(urlunsplit((parts.scheme, parts.netloc, parts.path,
                                            urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                                                       if k.lower() not in SECRET_QUERY_KEYS]), "")))
            identifier = post["id"]
            if not isinstance(identifier, str) or not identifier:
                continue
        except (KeyError, ValueError, AttributeError, TypeError, OverflowError):
            continue
        attachments = post.get("attachments")
        leaves = attachment_leaves(complete_collection_data(attachments))
        if not leaves or len({kind for kind, value in leaves}) != 1:
            continue
        kind = leaves[0][0]
        if kind == "video" and len(leaves) != 1:
            continue
        if kind in dates and published <= dates[kind]:
            continue
        item = {"sourceId": identifier, "sourceUrl": source_url, "publishedAt": published.isoformat(),
                "title": post.get("message") if isinstance(post.get("message"), str) and post["message"].strip() else None}
        if kind == "photos":
            item["photos"] = list(dict.fromkeys(value for _, value in leaves))
        else:
            item["videoId"] = leaves[0][1]
        selected[kind], dates[kind] = item, published
    return selected


def download_native(url, directory, index, kind):
    request = Request(checked_url(url), headers={"Accept": "image/*" if kind == "photos" else "video/mp4"})
    try:
        with urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if kind == "video":
                extension = ".mp4"
            else:
                extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type)
                if extension is None:
                    extension = Path(urlsplit(url).path).suffix.lower()
                if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                    raise FetchError("Facebook 原生照片格式不受支援", reason="unsupported_media")
            path = directory / ("{:02d}".format(index) + extension)
            with path.open("xb") as output:
                shutil.copyfileobj(response, output)
            if not path.stat().st_size:
                raise FetchError("Facebook 原生媒體下載為空", reason="download_failure")
            return path
    except FetchError:
        raise
    except (URLError, OSError, ValueError):
        raise FetchError("Facebook 原生媒體下載失敗；未匯入此貼文", reason="download_failure") from None


def record_status(root, status):
    with manifest_lock(root):
        manifest = load_manifest(root)
        manifest["fetchStatus"] = status
        save_manifest(root, manifest)
    return status


def configured(environ, lookback_days, max_pages):
    token = environ.get("FB_ACCESS_TOKEN", "").strip()
    version = environ.get("FB_GRAPH_VERSION", "").strip()
    page = environ.get("FB_PAGE_ID", DEFAULT_PAGE_ID).strip()
    if not token or not version:
        raise FetchError("請先在本機匯出 FB_ACCESS_TOKEN 與 FB_GRAPH_VERSION（Meta 應用程式已啟用的版本）；勿將權杖貼到前端或對話", "blocked", "configuration")
    if any(ord(c) < 33 or ord(c) == 127 for c in token) or not re.fullmatch(r"v[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[0-9]+", page):
        raise FetchError("請確認 FB_ACCESS_TOKEN、FB_GRAPH_VERSION（如 v主版.次版）與 FB_PAGE_ID 的本機設定", "blocked", "configuration")
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or not 1 <= lookback_days <= 36500 or isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise FetchError("lookback-days 必須為 1–36500，max-pages 必須為 1–100", "blocked", "configuration")
    return token, version, page


def parse_environment_content(content):
    """Parse one verified byte snapshot; no expansion or additional file reads."""
    message = "設定檔格式無效；只接受列出的 FB 設定逐行字面值"
    try:
        if not isinstance(content, bytes) or len(content) > 65536:
            raise ValueError()
        text = content.decode("utf-8")
        if any((ord(c) < 32 and c not in "\r\n") or ord(c) == 127 for c in text):
            raise ValueError()
        result = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key not in ENV_KEYS or key in result:
                raise ValueError()
            if value[:1] in {"'", '"'}:
                quote = value[0]
                if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
                    raise ValueError()
                value = value[1:-1]
            result[key] = value
        return result
    except (ValueError, TypeError, UnicodeError):
        raise FetchError(message, "blocked", "configuration") from None


def load_environment_file(filename):
    """Read only an explicitly supplied regular file; assignments are literal."""
    message = "明確指定的設定檔不存在、不安全或格式無效；只接受列出的 FB 設定逐行字面值"
    try:
        name = os.fspath(filename)
        if not isinstance(name, str) or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError()
        path = Path(name)
        if path.is_symlink():
            raise ValueError()
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError()
            content = handle.read(65537)
        return parse_environment_content(content)
    except (OSError, ValueError, TypeError, UnicodeError):
        raise FetchError(message, "blocked", "configuration") from None


def fetch(root=ROOT, lookback_days=30, max_pages=4, environ=None, now=None, env_file=None):
    """Use exports or one explicit literal file; preserve prior posts on failure."""
    completed = []
    failures = []
    try:
        exported = os.environ if environ is None else environ
        values = load_environment_file(env_file) if env_file is not None else {}
        for key in ENV_KEYS:
            if key in exported:
                values[key] = exported[key]
        token, version, page = configured(values, lookback_days, max_pages)
        clock = now if now is not None else datetime.now(timezone.utc)
        since = int((clock - timedelta(days=lookback_days)).timestamp())
        url = "https://graph.facebook.com/{}/{}/posts?{}".format(version, page, urlencode({"fields": FIELDS, "since": since, "limit": 25}))
        posts = []
        visited = set()
        for index in range(max_pages):
            if url in visited:
                break
            visited.add(url)
            body = graph_json(url, token)
            if not isinstance(body.get("data"), list):
                raise FetchError("Facebook 貼文回應缺少有效 data 清單", reason="invalid_response")
            posts.extend(body["data"])
            paging = body.get("paging")
            next_url = paging.get("next") if isinstance(paging, dict) else None
            if not next_url or index + 1 >= max_pages:
                break
            url = checked_url(next_url, graph=True)
            if not urlsplit(url).path.startswith("/" + version + "/"):
                raise FetchError("Facebook 分頁未使用指定 API 版本", reason="unsafe_url")
        posts = [{**post, "attachments": complete_attachments(post["attachments"], token, version, max_pages)}
                 if isinstance(post, dict) and isinstance(post.get("attachments"), dict) else post for post in posts]
        selected = select_posts(posts, since)
        for kind in ("photos", "video"):
            item = selected.get(kind)
            if item is None:
                continue
            identity = "facebook:" + item["sourceId"]
            manifest = load_manifest(root)
            expected_count = len(item["photos"]) if kind == "photos" else 1
            if any((p.get("sourceIdentity") == identity or p.get("sourceUrl") == item["sourceUrl"])
                   and originals_available(root, p, kind, expected_count) for p in manifest["posts"]):
                completed.append(kind)
                continue
            try:
                if kind == "photos":
                    sources = item["photos"]
                else:
                    body = graph_json("https://graph.facebook.com/{}/{}?fields=id,source,length".format(version, item["videoId"]), token)
                    source = body.get("source")
                    if not isinstance(source, str) or not source:
                        raise FetchError("Facebook 未提供原生影片 source；影片目前無法下載", "partial", "video_unavailable")
                    sources = [source]
                directory = data_directory(root, create=True)
                with tempfile.TemporaryDirectory(prefix=".facebook-", dir=str(directory)) as temporary:
                    files = [download_native(source, Path(temporary), index, kind) for index, source in enumerate(sources, 1)]
                    import_media(root, kind, item["sourceUrl"], files, title=item["title"],
                                 published_at=item["publishedAt"], source_id=identity)
                completed.append(kind)
            except FetchError as error:
                failures.append(error)
            except (MediaError, OSError):
                failures.append(FetchError("Facebook 下載檔案未通過本機匯入驗證；未新增此貼文", reason="import_failure"))
        if len(completed) == 2:
            status = {"state": "complete", "message": "已取得最近的照片貼文與影片貼文", "reason": "two_types_available"}
        elif completed:
            status = {"state": "partial", "message": "已取得部分貼文；" + (failures[0].args[0] if failures else "本次範圍內未找到另一種純媒體貼文"),
                      "reason": failures[0].reason if failures else "missing_post_type"}
        elif failures:
            status = {"state": failures[0].state, "message": str(failures[0]), "reason": failures[0].reason}
        else:
            status = {"state": "partial", "message": "查詢成功，但本次範圍內未找到純照片或純影片貼文", "reason": "no_matching_posts"}
    except FetchError as error:
        status = {"state": error.state, "message": str(error), "reason": error.reason}
    return record_status(root, status)


def main(argv=None):
    parser = argparse.ArgumentParser(description="以本機設定的官方 Facebook Graph API 權限匯入兩種貼文")
    parser.add_argument("--root", type=Path, default=ROOT, help="專案根目錄（預設為腳本所在專案）")
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--lookback-days", type=int, default=30)
    fetch_parser.add_argument("--max-pages", type=int, default=4, help="貼文查詢及每個附件／相簿集合的分頁上限（1–100）；附件未讀完則回報錯誤")
    fetch_parser.add_argument("--env-file", type=Path, help="明確指定本機 FB 字面值設定檔；不執行 shell，已匯出的環境變數優先")
    arguments = parser.parse_args(argv)
    try:
        status = fetch(arguments.root, arguments.lookback_days, arguments.max_pages, env_file=arguments.env_file)
    except (MediaError, OSError):
        print("無法安全更新本機 manifest；請確認 data 目錄與檔案權限", file=sys.stderr)
        return 1
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["state"] == "complete" else 2 if status["state"] == "partial" else 1


if __name__ == "__main__":
    sys.exit(main())
