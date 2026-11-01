#!/usr/bin/env python3
"""Import originals, optionally download one explicit video, and export candidates."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit
import uuid

from common import (MediaError, SECRET_QUERY_KEYS, data_directory, finite_number, load_manifest,
                    manifest_lock, merge_intervals, safe_data_path, save_manifest)

ROOT = Path(__file__).resolve().parents[1]
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def validate_source_url(url, video=False):
    if not isinstance(url, str) or any(character.isspace() or ord(character) < 32 for character in url):
        raise MediaError("請提供有效的 Facebook HTTPS 來源網址")
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}:
            raise ValueError()
        if parts.username is not None or parts.password is not None or parts.port is not None or "\\" in url:
            raise ValueError()
        decoded = unquote(parts.path)
        if any(part in {".", ".."} for part in decoded.split("/")) or "\\" in decoded:
            raise ValueError()
    except ValueError:
        raise MediaError("只接受沒有帳密或自訂埠號的 Facebook HTTPS 網址")
    if any(key.lower() in SECRET_QUERY_KEYS for key, value in parse_qsl(parts.query, keep_blank_values=True)):
        raise MediaError("Facebook 來源網址不能包含權杖或其他私密查詢參數；請提供移除私密參數的貼文網址")
    if video:
        query = parse_qs(parts.query)
        numeric_video = bool(re.fullmatch(r"[0-9]+", query.get("v", [""])[0]))
        explicit = ((parts.path.rstrip("/") in {"/watch", "/video.php"} and numeric_video)
                    or bool(re.fullmatch(r"/(?:[^/]+/)?videos/[0-9]+/?", parts.path))
                    or bool(re.fullmatch(r"/reel/[0-9]+/?", parts.path)))
        if not explicit:
            raise MediaError("download-video 需要明確的 Facebook watch、videos 或 reel 影片網址")
    return url


def validate_signature(handle, extension, kind):
    header = handle.read(64)
    handle.seek(0)
    photo = ((extension in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
             or (extension == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
             or (extension == ".webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP"))
    video = extension == ".mp4" and len(header) >= 16 and header[4:8] == b"ftyp"
    if not ((kind == "photos" and photo) or (kind == "video" and video)):
        raise MediaError("檔案副檔名或內容簽章與指定媒體類型不符；照片使用 JPG/PNG/WebP，影片使用 MP4")


def originals_available(root, post, kind, expected_count):
    """An identity is reusable only when its expected safe local originals exist."""
    if not isinstance(post, dict) or post.get("kind") != kind:
        return False
    assets = post.get("assets")
    if not isinstance(assets, list) or not assets or len(assets) != expected_count:
        return False
    seen = set()
    try:
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("type") != ("image" if kind == "photos" else "video"):
                return False
            path = safe_data_path(root, asset.get("path"), allowed={"media"})
            if path in seen:
                return False
            seen.add(path)
            descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return False
                validate_signature(handle, path.suffix.lower(), kind)
    except (MediaError, OSError):
        return False
    return True


def publication_date(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise MediaError("published-at 請使用 ISO 8601 日期時間；未知時省略")
    if parsed.tzinfo is None:
        raise MediaError("published-at 日期時間必須包含時區")
    return parsed.isoformat()


def generated_id(prefix):
    return "{}-{}".format(prefix, uuid.uuid4().hex)


def embed_video(root, url, title=None, published_at=None):
    """Record an explicit Facebook video source without fetching or processing it."""
    validate_source_url(url, video=True)
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path in {"/watch", "/video.php"}:
        video_id = parse_qs(parts.query)["v"][0]
        query = urlencode({"v": video_id})
        path = "/watch/" if path == "/watch" else "/video.php"
    else:
        video_id = path.rsplit("/", 1)[1]
        if re.fullmatch(r"/(?:[^/]+/)?videos/[0-9]+", path):
            path = "/watch/"
            query = urlencode({"v": video_id})
        else:
            path += "/"
            query = ""
    source = urlunsplit(("https", "www.facebook.com", path, query, ""))
    published = publication_date(published_at)
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise MediaError("貼文標題不能空白")
    identity = "facebook-embed:" + video_id
    with manifest_lock(root):
        manifest = load_manifest(root)
        existing = next((post for post in manifest["posts"]
                         if post.get("playback") == "facebook_embed" and
                         (post.get("sourceIdentity") == identity or post.get("sourceUrl") == source)), None)
        if existing is not None:
            return existing
        post = {"id": generated_id("post"), "kind": "video", "playback": "facebook_embed",
                "sourceUrl": source, "sourceIdentity": identity, "title": title or "Facebook 影片",
                "publishedAt": published, "importedAt": datetime.now(timezone.utc).isoformat(),
                "assets": [], "recognition": {"state": "not_required",
                "message": "影片由 Facebook 播放器提供，未執行內容辨識。", "results": []}, "clips": []}
        manifest["posts"].append(post)
        save_manifest(root, manifest)
        return post


def import_media(root, kind, source_url, files, title=None, published_at=None, fetch_status=None, source_id=None,
                 before_commit=None):
    validate_source_url(source_url)
    if kind not in {"photos", "video"} or not files or (kind == "video" and len(files) != 1):
        raise MediaError("照片需至少一個檔案；video 每篇需一個 MP4 檔案")
    published = publication_date(published_at)
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise MediaError("貼文標題不能空白")
    if source_id is not None and (not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200
                                  or any(ord(character) < 32 for character in source_id)):
        raise MediaError("無效的來源識別碼")
    # Keep the exact validated file descriptors open until copying completes.
    with ExitStack() as handles:
        originals = []
        reference_directory = (Path(root).resolve() / "data/reference-child").resolve()
        for filename in files:
            path = Path(filename)
            extension = path.suffix.lower()
            try:
                path.resolve().relative_to(reference_directory)
            except ValueError:
                pass
            else:
                raise MediaError("孩子的參考照片只供本機比對，不能匯入公開相簿")
            if path.is_symlink():
                raise MediaError("匯入檔案不能是符號連結")
            try:
                descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                handle = handles.enter_context(os.fdopen(descriptor, "rb"))
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise MediaError("匯入來源必須是一般檔案")
                validate_signature(handle, extension, kind)
            except OSError as error:
                raise MediaError("無法開啟匯入檔案：{}".format(path.name)) from error
            originals.append((handle, extension))
        with manifest_lock(root):
            # A caller can abort under the same lock that protects installation
            # and manifest publication; it must raise to reject the import.
            if before_commit is not None:
                before_commit()
            manifest = load_manifest(root)
            existing_index = None
            if source_id is not None:
                existing_index = next((index for index, post in enumerate(manifest["posts"])
                                       if post.get("sourceIdentity") == source_id or post.get("sourceUrl") == source_url), None)
                if existing_index is not None and originals_available(root, manifest["posts"][existing_index], kind, len(files)):
                    return manifest["posts"][existing_index]
            media_dir = safe_data_path(root, "media")
            media_dir.mkdir(exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".import-", dir=str(media_dir)))
            post_id = generated_id("post")
            destination = safe_data_path(root, "media/" + post_id)
            installed = False
            try:
                assets = []
                for index, (handle, extension) in enumerate(originals, 1):
                    name = "{:02d}{}".format(index, ".jpg" if extension == ".jpeg" else extension)
                    with (stage / name).open("xb") as output:
                        shutil.copyfileobj(handle, output)
                        output.flush()
                        os.fsync(output.fileno())
                    assets.append({"id": generated_id("asset"), "type": "image" if kind == "photos" else "video",
                                   "path": "media/{}/{}".format(post_id, name), "name": name})
                post = {"id": post_id, "kind": kind, "sourceUrl": source_url,
                        "title": title or ("照片貼文" if kind == "photos" else "影片貼文"),
                        "publishedAt": published, "importedAt": datetime.now(timezone.utc).isoformat(),
                        "assets": assets, "recognition": {"state": "pending", "message": "等待參考照片與本機辨識", "results": []},
                        "clips": []}
                if source_id is not None:
                    post["sourceIdentity"] = source_id
                os.rename(str(stage), str(destination))
                installed = True
                if existing_index is None:
                    manifest["posts"].append(post)
                else:
                    manifest["posts"][existing_index] = post
                if fetch_status is not None:
                    manifest["fetchStatus"] = fetch_status
                save_manifest(root, manifest)
                return post
            except Exception:
                shutil.rmtree(destination if installed else stage, ignore_errors=True)
                raise


def find_ffmpeg():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    if importlib.util.find_spec("imageio_ffmpeg") is not None:
        # Only accept the wheel's bundled executable, never fetch one or inspect
        # arbitrary credentials/configuration to locate an executable.
        import imageio_ffmpeg
        binary_dir = Path(imageio_ffmpeg.__file__).resolve().parent / "binaries"
        for candidate in sorted(binary_dir.glob("ffmpeg-*")):
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                return str(candidate)
    return None


def find_ytdlp():
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def record_fetch_failure(root, message):
    with manifest_lock(root):
        manifest = load_manifest(root)
        manifest["fetchStatus"] = {"state": "failed", "message": message}
        save_manifest(root, manifest)


def download_video(root, url, cookies=None):
    validate_source_url(url, video=True)
    if cookies is not None:
        cookies = Path(cookies).resolve()
        if not cookies.is_file():
            raise MediaError("明確指定的 cookies 檔案不存在")
    executable = find_ytdlp()
    if executable is None:
        message = "缺少 yt-dlp；請自行安裝 requirements-optional.txt 的 yt-dlp"
        record_fetch_failure(root, message)
        raise MediaError(message)
    data = data_directory(root, create=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=str(data)) as temporary:
        output_template = str(Path(temporary) / "original.%(ext)s")
        ffmpeg = find_ffmpeg()
        argv = executable + ["--ignore-config", "--no-playlist", "--no-cache-dir", "--no-progress", "--quiet",
                             "--restrict-filenames", "--output", output_template,
                             "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]" if ffmpeg else "best[ext=mp4]",
                             "--merge-output-format", "mp4"]
        if ffmpeg:
            argv.extend(["--ffmpeg-location", ffmpeg])
        if cookies:
            argv.extend(["--cookies", str(cookies)])
        argv.extend(["--", url])
        try:
            subprocess.run(argv, check=True, capture_output=True, text=True, timeout=1800)
            output = Path(temporary) / "original.mp4"
            if not output.is_file() or output.is_symlink():
                raise MediaError("yt-dlp 未產生可匯入的 original.mp4")
            return import_media(root, "video", url, [output], fetch_status={"state": "complete", "message": "已下載此明確指定的 Facebook 影片"})
        except (OSError, subprocess.SubprocessError, MediaError) as error:
            # Never expose downloader stderr: it may contain supplied cookies or
            # signed URLs. Existing assets and posts remain intact.
            message = "Facebook 影片下載失敗（{}）；原有媒體已保留".format(type(error).__name__)
            record_fetch_failure(root, message)
            raise MediaError(message) from error


def clip_post(root, post_id):
    if not isinstance(post_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", post_id):
        raise MediaError("無效的貼文 ID")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise MediaError("缺少 FFmpeg；請安裝 ffmpeg 或 requirements-optional.txt 的 imageio-ffmpeg")
    with manifest_lock(root):
        manifest = load_manifest(root)
        post = next((item for item in manifest["posts"] if item.get("id") == post_id), None)
        if post is None or post.get("kind") != "video":
            raise MediaError("找不到此影片貼文")
        if post.get("recognition", {}).get("state") != "complete":
            raise MediaError("尚未完成本機候選辨識，無法匯出片段")
        assets = {asset["id"]: asset for asset in post.get("assets", []) if asset.get("type") == "video"}
        jobs = []
        for result in post.get("recognition", {}).get("results", []):
            if result.get("state") != "candidate":
                continue
            asset = assets.get(result.get("assetId"))
            if asset is None:
                raise MediaError("辨識結果引用不存在的影片")
            source = safe_data_path(root, asset.get("path"), allowed={"media"})
            if not source.is_file():
                raise MediaError("原始影片檔案不存在")
            intervals = merge_intervals(result.get("segments", []), result.get("duration"), gap=0)
            jobs.extend((asset, source, segment) for segment in intervals)
        if not jobs:
            raise MediaError("沒有可匯出的候選時間片段")
        if len(jobs) > 200:
            raise MediaError("候選片段超過 200 個，請先調整辨識取樣與合併間隔")
        clips_dir = safe_data_path(root, "clips")
        clips_dir.mkdir(exist_ok=True)
        batch_id = generated_id("batch")
        stage = Path(tempfile.mkdtemp(prefix=".clip-", dir=str(clips_dir)))
        destination = safe_data_path(root, "clips/" + batch_id)
        installed = False
        try:
            clips = []
            for index, (asset, source, interval) in enumerate(jobs, 1):
                name = "candidate-{:03d}.mp4".format(index)
                output = stage / name
                argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                        "-protocol_whitelist", "file,pipe", "-i", str(source),
                        "-ss", "{:.6f}".format(interval["start"]), "-t", "{:.6f}".format(interval["end"] - interval["start"]),
                        "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                        "-c:a", "aac", "-movflags", "+faststart", str(output)]
                subprocess.run(argv, check=True, capture_output=True, text=True, timeout=1800)
                if not output.is_file() or output.is_symlink():
                    raise MediaError("FFmpeg 未產生片段")
                with output.open("rb") as handle:
                    validate_signature(handle, ".mp4", "video")
                clips.append({"id": generated_id("clip"), "assetId": asset["id"], "type": "video",
                              "path": "clips/{}/{}".format(batch_id, name), "name": name,
                              "start": interval["start"], "end": interval["end"], "state": "candidate"})
            os.rename(str(stage), str(destination))
            installed = True
            post.setdefault("clips", []).extend(clips)
            save_manifest(root, manifest)
            return clips
        except Exception as error:
            shutil.rmtree(destination if installed else stage, ignore_errors=True)
            if isinstance(error, (OSError, subprocess.SubprocessError)):
                raise MediaError("FFmpeg 匯出失敗（{}）；原有 manifest 與片段已保留".format(type(error).__name__)) from error
            raise


def doctor(root):
    """Inspect local installed files only; no installs, model or media fetches."""
    references = safe_data_path(root, "reference-child")
    count = sum(1 for path in references.iterdir() if path.is_file() and not path.is_symlink() and path.suffix.lower() in PHOTO_EXTENSIONS) if references.is_dir() else 0
    models = safe_data_path(root, "models")
    def installed(module):
        return importlib.util.find_spec(module) is not None
    manifest = load_manifest(root)
    return {"python": sys.version.split()[0], "dependencies": {"yt-dlp": find_ytdlp() is not None,
            "ffmpeg": find_ffmpeg() is not None, "opencv": installed("cv2"), "numpy": installed("numpy")},
            "reference": {"count": count, "directory": "data/reference-child"},
            "models": {"directory": "data/models", "files": sorted(path.name for path in models.glob("*.onnx") if path.is_file() and not path.is_symlink()) if models.is_dir() else []},
            "posts": len(manifest["posts"]), "fetchStatus": manifest.get("fetchStatus")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="SunnyEgg 本機原始媒體與候選片段工具")
    parser.add_argument("--root", type=Path, default=ROOT, help="專案根目錄（預設由腳本位置決定）")
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="複製使用者已下載的原始照片或 MP4")
    importer.add_argument("--kind", choices=["photos", "video"], required=True)
    importer.add_argument("--source-url", required=True)
    importer.add_argument("--files", nargs="+", required=True, type=Path)
    importer.add_argument("--title")
    importer.add_argument("--published-at", help="已確認的 ISO 8601 日期時間（含時區）；未知則省略")
    downloader = commands.add_parser("download-video", help="以選用 yt-dlp 下載明確指定的 Facebook 影片")
    downloader.add_argument("--url", required=True)
    downloader.add_argument("--cookies", type=Path, help="僅讀取使用者明確指定的 cookies 檔案；不擷取瀏覽器 cookies")
    embedder = commands.add_parser("embed-video", help="記錄 Facebook 影片來源供網頁內嵌，不下載或辨識影片")
    embedder.add_argument("--url", required=True)
    embedder.add_argument("--title")
    embedder.add_argument("--published-at", help="已確認的 ISO 8601 日期時間（含時區）；未知則省略")
    clipper = commands.add_parser("clip", help="以 FFmpeg 重新編碼匯出本機辨識候選片段")
    clipper.add_argument("--post-id", required=True)
    commands.add_parser("doctor", help="只檢查本機相依套件、模型與參考照片")
    commands.add_parser("status", help="顯示本機狀態，與 doctor 相同")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            result = import_media(args.root, args.kind, args.source_url, args.files, args.title, args.published_at)
        elif args.command == "download-video":
            result = download_video(args.root, args.url, args.cookies)
        elif args.command == "embed-video":
            result = embed_video(args.root, args.url, args.title, args.published_at)
        elif args.command == "clip":
            result = clip_post(args.root, args.post_id)
        else:
            result = doctor(args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (MediaError, OSError) as error:
        print("錯誤：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
