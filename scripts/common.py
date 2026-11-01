"""Shared local-only manifest and time interval helpers (Python 3.9+)."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile

SOURCE_PAGE = "https://www.facebook.com/profile.php?id=100063963161862"
SECRET_QUERY_KEYS = {"access_token", "appsecret_proof", "oauth_token", "token", "authorization"}


class MediaError(ValueError):
    """An actionable local media error."""


def data_directory(root, create=False):
    root = Path(root).resolve()
    directory = root / "data"
    if directory.is_symlink():
        raise MediaError("data 目錄不能是符號連結")
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    if directory.exists() and not directory.is_dir():
        raise MediaError("data 必須是目錄")
    return directory


def safe_data_path(root, relative, allowed=None):
    """Reject traversal and symlink components, even links within data/."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise MediaError("無效的媒體路徑")
    path = Path(relative)
    if path.is_absolute() or any(part in (".", "..") for part in relative.split("/")):
        raise MediaError("媒體路徑不能離開 data 目錄")
    if allowed is not None and path.parts[0] not in allowed:
        raise MediaError("不允許讀取此資料目錄")
    directory = data_directory(root)
    target = directory
    for component in path.parts:
        target = target / component
        if target.is_symlink():
            raise MediaError("媒體路徑不能包含符號連結")
    try:
        target.resolve().relative_to(directory.resolve())
    except ValueError:
        raise MediaError("媒體路徑不能離開 data 目錄")
    return target


def default_manifest():
    return {"version": 1, "sourcePage": SOURCE_PAGE,
            "fetchStatus": {"state": "blocked", "message": "Facebook 讀取尚未成功"}, "posts": []}


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("posts"), list):
        raise MediaError("不支援或損壞的 manifest 格式")


def load_manifest(root):
    path = safe_data_path(root, "manifest.json")
    if not path.exists():
        return default_manifest()
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            manifest = json.load(handle, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError) as error:
        raise MediaError("無法讀取 manifest：{}".format(error)) from error
    validate_manifest(manifest)
    return manifest


@contextmanager
def manifest_lock(root):
    """Hold across reload, mutation, and save to avoid lost concurrent updates."""
    data_directory(root, create=True)
    path = safe_data_path(root, ".manifest.lock")
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def save_manifest(root, manifest):
    """Atomic replacement; callers hold manifest_lock for read-modify-write."""
    validate_manifest(manifest)
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    directory = data_directory(root, create=True)
    destination = safe_data_path(root, "manifest.json")
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=str(directory))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(destination))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def merge_segments(hits, duration, padding=2, gap=2):
    """Expand hits with strictly positive padding; gap may be zero."""
    duration, padding, gap = finite_number(duration), finite_number(padding), finite_number(gap)
    if padding is None or padding <= 0:
        raise MediaError("padding 命中前後秒數必須大於 0")
    if duration is None or duration <= 0 or gap is None or gap < 0:
        raise MediaError("片段長度、前後秒數或合併間隔無效")
    intervals = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        time, score = finite_number(hit.get("time")), finite_number(hit.get("score"))
        if time is None or score is None or time < 0 or time > duration:
            continue
        start, end = max(0.0, time - padding), min(duration, time + padding)
        if end > start:
            intervals.append({"start": start, "end": end, "score": score})
    return merge_intervals(intervals, duration, gap)


def merge_intervals(segments, duration, gap=0):
    """Normalize already expanded intervals without applying padding twice."""
    duration, gap = finite_number(duration), finite_number(gap)
    if duration is None or duration <= 0 or gap is None or gap < 0:
        raise MediaError("影片長度或合併間隔無效")
    intervals = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        start, end, score = (finite_number(segment.get(key)) for key in ("start", "end", "score"))
        if start is None or end is None or score is None:
            continue
        start, end = max(0.0, start), min(duration, end)
        if end > start:
            intervals.append({"start": start, "end": end, "score": score})
    merged = []
    for interval in sorted(intervals, key=lambda value: (value["start"], value["end"])):
        if merged and interval["start"] <= merged[-1]["end"] + gap:
            merged[-1]["end"] = max(merged[-1]["end"], interval["end"])
            merged[-1]["score"] = max(merged[-1]["score"], interval["score"])
        else:
            merged.append(interval.copy())
    return merged
