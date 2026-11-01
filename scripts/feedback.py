#!/usr/bin/env python3
"""Reversible local parent labels; private negative images, never face vectors."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from common import MediaError, default_manifest, load_manifest, manifest_lock, safe_data_path, save_manifest
from media import PHOTO_EXTENSIONS, validate_signature

ROOT = Path(__file__).resolve().parents[1]
ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,200}')
MAX_FEEDBACK_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
ERRORS = {
    'invalid_input': (400, '請提供有效的照片識別碼與排除狀態。'),
    'not_found': (404, '找不到指定的本機照片。'),
    'invalid_original': (409, '照片原始檔無法驗證，尚未保存標記。'),
    'feedback_changed': (409, '照片標記已更新，請重新抓取後重試。'),
    'local_storage': (503, '照片標記尚未確認保存，請重新讀取後重試。'),
}


class FeedbackError(MediaError):
    def __init__(self, reason='local_storage'):
        self.reason = reason if reason in ERRORS else 'local_storage'
        self.status_code, message = ERRORS[self.reason]
        super().__init__(message)


def _valid_id(value):
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def _regular_bytes(path, limit):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise FeedbackError()
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise FeedbackError()
    return value


def load_feedback(root):
    """Read only the safe private metadata contract, including disabled history."""
    try:
        path = safe_data_path(root, 'feedback.json')
        if not path.exists():
            return {'version': 1, 'revision': 0, 'examples': {}}
        body = json.loads(_regular_bytes(path, MAX_FEEDBACK_BYTES).decode('utf-8'))
        if (not isinstance(body, dict) or set(body) != {'version', 'revision', 'examples'} or type(body['version']) is not int or body['version'] != 1
                or isinstance(body['revision'], bool) or not isinstance(body['revision'], int) or body['revision'] < 0
                or not isinstance(body['examples'], dict) or len(body['examples']) > 10000):
            raise ValueError()
        for asset_id, item in body['examples'].items():
            if (not _valid_id(asset_id) or not isinstance(item, dict)
                    or not {'postId', 'assetId', 'excluded', 'imageHash', 'path', 'updatedAt'}.issubset(item)
                    or set(item) - {'postId', 'assetId', 'excluded', 'imageHash', 'path', 'updatedAt', 'photoId'}
                    or item['assetId'] != asset_id or not _valid_id(item['postId']) or not isinstance(item['excluded'], bool)
                    or not isinstance(item['imageHash'], str) or re.fullmatch(r'[a-f0-9]{64}', item['imageHash']) is None
                    or item['path'] not in ['negative-examples/' + item['imageHash'] + extension for extension in ('.jpg', '.png', '.webp')]):
                raise ValueError()
            safe_data_path(root, item['path'], allowed={'negative-examples'})
            timestamp = datetime.fromisoformat(item['updatedAt'])
            if timestamp.tzinfo is None:
                raise ValueError()
            if 'photoId' in item and (not isinstance(item['photoId'], str) or re.fullmatch(r'[0-9]+', item['photoId']) is None):
                raise ValueError()
        return body
    except FeedbackError:
        raise
    except (MediaError, OSError, ValueError, TypeError, UnicodeError):
        raise FeedbackError() from None


def _atomic_bytes(root, relative, value):
    target = safe_data_path(root, relative)
    descriptor, temporary = tempfile.mkstemp(prefix='.feedback-', dir=str(target.parent))
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _private_copy(root, digest, extension, contents):
    directory = safe_data_path(root, 'negative-examples')
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    relative = 'negative-examples/' + digest + extension
    path = safe_data_path(root, relative, allowed={'negative-examples'})
    if path.exists():
        if hashlib.sha256(_regular_bytes(path, MAX_IMAGE_BYTES)).hexdigest() != digest:
            raise FeedbackError()
    else:
        _atomic_bytes(root, relative, contents)
    return relative


def _photo_id(post, asset):
    photo_id = asset.get('sourcePhotoId')
    photo_ids = post.get('dailyPhotoIds')
    if photo_id is None and isinstance(photo_ids, list) and len(photo_ids) == len(post.get('assets', [])):
        photo_id = photo_ids[post['assets'].index(asset)]
    return photo_id if isinstance(photo_id, str) and re.fullmatch(r'[0-9]+', photo_id) else None


def _identity_index(manifest):
    """Use known photo IDs or complete, unambiguous alias chains as evidence."""
    occurrences = {}
    for post in manifest['posts']:
        if not isinstance(post, dict) or post.get('kind') != 'photos' or not _valid_id(post.get('id')):
            continue
        for asset in post.get('assets', []):
            if isinstance(asset, dict) and asset.get('type') == 'image' and _valid_id(asset.get('id')):
                occurrences.setdefault(asset['id'], []).append((post, asset))
    members = {asset_id: values[0] for asset_id, values in occurrences.items() if len(values) == 1}
    parent = {asset_id: asset_id for asset_id in members}
    def find(asset_id):
        while parent[asset_id] != asset_id:
            parent[asset_id] = parent[parent[asset_id]]
            asset_id = parent[asset_id]
        return asset_id
    def union(first, second):
        parent[find(second)] = find(first)
    known = {}
    for asset_id, (post, asset) in members.items():
        photo_id = _photo_id(post, asset)
        if photo_id is not None:
            if photo_id in known:
                union(known[photo_id], asset_id)
            else:
                known[photo_id] = asset_id
    chains = {}
    for asset_id in members:
        path, seen, current = [], set(), asset_id
        while current in members and current not in seen:
            seen.add(current)
            path.append(current)
            target = members[current][1].get('duplicateOf')
            if target is None:
                chains.setdefault(current, []).append(path)
                break
            if not _valid_id(target):
                break
            current = target
    valid_aliases = set()
    for canonical, paths in chains.items():
        aliases = set(asset_id for path in paths for asset_id in path)
        photo_ids = {_photo_id(*members[asset_id]) for asset_id in aliases} - {None}
        if len(photo_ids) <= 1:
            for asset_id in aliases:
                union(canonical, asset_id)
                if asset_id != canonical:
                    valid_aliases.add(asset_id)
    groups = {}
    for asset_id in members:
        groups.setdefault(find(asset_id), []).append(members[asset_id])
    return {asset['id']: group for group in groups.values() for _, asset in group}, valid_aliases


def _logical_groups(manifest):
    return _identity_index(manifest)[0]


def source_photo_id(post, asset):
    """Return the valid, current numeric source identity without private reads."""
    return _photo_id(post, asset)


def identity_groups(manifest):
    """Shared current identity policy; returns asset ID to member tuple lists."""
    return _logical_groups(manifest)


def identity_canonicals(manifest):
    """Map each member to its owner using aliases validated in the full family."""
    groups, valid_aliases = _identity_index(manifest)
    owners, result = {}, {}
    for asset_id, group in groups.items():
        first_id = group[0][1]['id']
        if first_id not in owners:
            owners[first_id] = next((member for member in group if member[1]['id'] not in valid_aliases), group[0])
        result[asset_id] = owners[first_id]
    return result


def _negative_count(body, groups):
    identities = set()
    for asset_id, item in body['examples'].items():
        if not item['excluded']:
            continue
        group = groups.get(asset_id, [])
        photo_ids = {_photo_id(post, asset) for post, asset in group} - {None}
        for post, asset in group:
            history = body['examples'].get(asset['id'])
            if history is not None and history['postId'] == post['id'] and history.get('photoId') is not None:
                photo_ids.add(history['photoId'])
        photo_id = item.get('photoId')
        if photo_id is not None:
            key = ('photo', photo_id)
        elif len(photo_ids) == 1:
            key = ('photo', next(iter(photo_ids)))
        else:
            key = ('asset', min(asset['id'] for _, asset in group) if group else asset_id)
        identities.add(key)
    return len(identities)


def commit_identity_backfills(root, manifest, backfills=None, *, before_commit=None, expected_feedback=None):
    """Commit proven identity metadata with gallery updates; caller holds manifest_lock.

    Supplied backfills were exact-byte verified by the daily caller. Current
    source IDs and valid alias groups can also prove an already-retained ID.
    The callback receives copies of validated old/new metadata and must only
    derive an in-memory profile; the caller adopts it after successful return.
    """
    root = Path(root).resolve()
    backfills = {} if backfills is None else backfills
    if (not isinstance(backfills, dict) or any(not _valid_id(asset_id)
            or not isinstance(photo_id, str) or re.fullmatch(r'[0-9]+', photo_id) is None
            for asset_id, photo_id in backfills.items())):
        raise FeedbackError('invalid_input')
    # Capture authoritative bytes before parsing either contract. A direct edit
    # after a first read must not be silently adopted as a later baseline.
    feedback_path = safe_data_path(root, 'feedback.json')
    old_feedback_bytes = _regular_bytes(feedback_path, MAX_FEEDBACK_BYTES) if feedback_path.exists() else None
    manifest_path = safe_data_path(root, 'manifest.json')
    old_manifest_bytes = _regular_bytes(manifest_path, 64 * 1024 * 1024) if manifest_path.exists() else None
    body = load_feedback(root)
    previous_manifest = load_manifest(root)
    try:
        snapshot_feedback = (json.loads(old_feedback_bytes.decode('utf-8')) if old_feedback_bytes is not None
                             else {'version': 1, 'revision': 0, 'examples': {}})
        snapshot_manifest = (json.loads(old_manifest_bytes.decode('utf-8')) if old_manifest_bytes is not None
                             else default_manifest())
    except (ValueError, UnicodeError):
        raise FeedbackError('feedback_changed') from None
    if body != snapshot_feedback or previous_manifest != snapshot_manifest:
        raise FeedbackError('feedback_changed')
    if expected_feedback is not None and body != expected_feedback:
        raise FeedbackError('feedback_changed')
    previous_body = deepcopy(body)
    old_groups = identity_groups(previous_manifest)
    groups = identity_groups(manifest)
    def member_by_id(group, asset_id):
        return next((member for member in group if member[1]['id'] == asset_id), None)
    for asset_id, photo_id in backfills.items():
        current = member_by_id(groups.get(asset_id, []), asset_id)
        original = member_by_id(old_groups.get(asset_id, []), asset_id)
        if (current is None or original is None or current[0]['id'] != original[0]['id']
                or current[1].get('path') != original[1].get('path')
                or current[1].get('userExcluded', False) != original[1].get('userExcluded', False)
                or source_photo_id(*current) != photo_id
                or source_photo_id(*original) not in (None, photo_id)):
            raise FeedbackError('invalid_original')
    changed = False
    for asset_id, item in body['examples'].items():
        group = groups.get(asset_id, [])
        current = member_by_id(group, asset_id)
        if current is None:
            continue  # Retained orphan history has no new proof to upgrade.
        original = member_by_id(old_groups.get(asset_id, []), asset_id)
        if (original is None or item['postId'] != current[0]['id'] or item['postId'] != original[0]['id']
                or current[1].get('path') != original[1].get('path')
                or current[1].get('userExcluded', False) != original[1].get('userExcluded', False)):
            raise FeedbackError('invalid_original')
        identifiers = {source_photo_id(post, asset) for post, asset in group} - {None}
        if len(identifiers) != 1:
            continue
        photo_id = next(iter(identifiers))
        if item.get('photoId') is not None:
            if item['photoId'] != photo_id:
                raise FeedbackError('invalid_original')
            continue
        try:
            source = safe_data_path(root, original[1].get('path'), allowed={'media'})
            if source.suffix.lower() not in PHOTO_EXTENSIONS:
                raise MediaError('invalid image extension')
            contents = _regular_bytes(source, MAX_IMAGE_BYTES)
            validate_signature(io.BytesIO(contents), source.suffix.lower(), 'photos')
            if hashlib.sha256(contents).hexdigest() != item['imageHash']:
                raise MediaError('original identity changed')
            if item['excluded']:
                private = safe_data_path(root, item['path'], allowed={'negative-examples'})
                if hashlib.sha256(_regular_bytes(private, MAX_IMAGE_BYTES)).hexdigest() != item['imageHash']:
                    raise MediaError('private snapshot changed')
        except (MediaError, OSError):
            raise FeedbackError('invalid_original') from None
        item['photoId'] = photo_id
        changed = True
    if changed:
        body['revision'] += 1
    encoded = (json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if len(encoded) > MAX_FEEDBACK_BYTES:
        raise FeedbackError()
    if before_commit is not None:
        before_commit(deepcopy(previous_body), deepcopy(body))
    current_feedback_bytes = _regular_bytes(feedback_path, MAX_FEEDBACK_BYTES) if feedback_path.exists() else None
    current_manifest_bytes = _regular_bytes(manifest_path, 64 * 1024 * 1024) if manifest_path.exists() else None
    if current_feedback_bytes != old_feedback_bytes or current_manifest_bytes != old_manifest_bytes:
        raise FeedbackError('feedback_changed')
    try:
        if changed:
            _atomic_bytes(root, 'feedback.json', encoded)
        save_manifest(root, manifest)
    except Exception:
        try:
            if old_feedback_bytes is None:
                feedback_path.unlink(missing_ok=True)
            else:
                _atomic_bytes(root, 'feedback.json', old_feedback_bytes)
            if old_manifest_bytes is None:
                manifest_path.unlink(missing_ok=True)
            else:
                _atomic_bytes(root, 'manifest.json', old_manifest_bytes)
        except (OSError, MediaError):
            pass
        raise FeedbackError() from None
    return body


def set_feedback(root, post_id, asset_id, excluded):
    """Serialize labels with the manifest; rollback paired writes on failure."""
    if not _valid_id(post_id) or not _valid_id(asset_id) or not isinstance(excluded, bool):
        raise FeedbackError('invalid_input')
    root = Path(root).resolve()
    try:
        with manifest_lock(root):
            body = load_feedback(root)
            manifest = load_manifest(root)
            matches = [post for post in manifest['posts'] if isinstance(post, dict) and post.get('id') == post_id and post.get('kind') == 'photos']
            if len(matches) != 1:
                raise FeedbackError('not_found')
            post = matches[0]
            assets = [asset for asset in post.get('assets', []) if isinstance(asset, dict) and asset.get('id') == asset_id and asset.get('type') == 'image']
            if len(assets) != 1:
                raise FeedbackError('not_found')
            asset = assets[0]
            groups = _logical_groups(manifest)
            group = groups.get(asset_id)
            if group is None:
                raise FeedbackError('not_found')
            photo_ids = {_photo_id(member_post, member) for member_post, member in group} - {None}
            photo_id = next(iter(photo_ids)) if len(photo_ids) == 1 else None
            histories = {}
            for member_post, member in group:
                old = body['examples'].get(member['id'])
                if old is not None:
                    if (old['postId'] != member_post['id']
                            or (photo_id is not None and old.get('photoId', photo_id) != photo_id)):
                        raise FeedbackError('invalid_original')
                    histories[member['id']] = old
            retained_ids = {old.get('photoId') for old in histories.values()} - {None}
            if len(photo_ids | retained_ids) > 1:
                # Removing current source metadata must not erase conflicting
                # identities that were already verified and privately retained.
                raise FeedbackError('invalid_original')
            record_photo_id = photo_id or (next(iter(retained_ids)) if retained_ids else None)
            if record_photo_id is not None:
                # Retained numeric identity proves equivalence even when today's
                # manifest has lost an ID or an entire photo. Expand flags and
                # histories together; contradictory current/history IDs stay out.
                expanded = {member['id']: (member_post, member) for member_post, member in group}
                candidates = {history_id for history_id, old in body['examples'].items()
                              if old.get('photoId') == record_photo_id}
                candidates.update(member_id for member_id, members in groups.items()
                                  if any(_photo_id(p, a) == record_photo_id for p, a in members))
                for history_id in candidates:
                    history_group = groups.get(history_id, [])
                    if not history_group:
                        histories[history_id] = body['examples'][history_id]
                        continue
                    current_ids = {_photo_id(p, a) for p, a in history_group} - {None}
                    group_history = {}
                    compatible = True
                    for member_post, member in history_group:
                        previous = body['examples'].get(member['id'])
                        if previous is not None:
                            if previous['postId'] != member_post['id']:
                                compatible = False
                                break
                            group_history[member['id']] = previous
                    current_ids.update(item.get('photoId') for item in group_history.values() if item.get('photoId') is not None)
                    if compatible and current_ids.issubset({record_photo_id}):
                        expanded.update({member['id']: (member_post, member) for member_post, member in history_group})
                        histories.update(group_history)
                group = list(expanded.values())
            now = datetime.now(timezone.utc).isoformat()
            changed = any(member.get('userExcluded', False) != excluded for _, member in group)
            if excluded:
                try:
                    source = safe_data_path(root, asset.get('path'), allowed={'media'})
                    if source.suffix.lower() not in PHOTO_EXTENSIONS:
                        raise MediaError('invalid image extension')
                    contents = _regular_bytes(source, MAX_IMAGE_BYTES)
                    validate_signature(io.BytesIO(contents), source.suffix.lower(), 'photos')
                except (MediaError, OSError):
                    raise FeedbackError('invalid_original') from None
                digest = hashlib.sha256(contents).hexdigest()
                extension = '.jpg' if source.suffix.lower() == '.jpeg' else source.suffix.lower()
                relative = _private_copy(root, digest, extension, contents)
                old = body['examples'].get(asset_id)
                if old is None or not old['excluded'] or old['imageHash'] != digest or old.get('photoId') != record_photo_id:
                    item = {'postId': post_id, 'assetId': asset_id, 'excluded': True, 'imageHash': digest,
                            'path': relative, 'updatedAt': now}
                    if record_photo_id is not None:
                        item['photoId'] = record_photo_id
                    body['examples'][asset_id] = item
                    changed = True
            else:
                # Undo is metadata-only for every known logical member; missing
                # or changed gallery/private files cannot trap active learning.
                for old in histories.values():
                    if old['excluded']:
                        old['excluded'] = False
                        old['updatedAt'] = now
                        changed = True
            if changed:
                feedback_path = safe_data_path(root, 'feedback.json')
                previous_feedback = _regular_bytes(feedback_path, MAX_FEEDBACK_BYTES) if feedback_path.exists() else None
                manifest_path = safe_data_path(root, 'manifest.json')
                previous_manifest = _regular_bytes(manifest_path, 64 * 1024 * 1024)
                body['revision'] += 1
                encoded = (json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
                if len(encoded) > MAX_FEEDBACK_BYTES:
                    raise FeedbackError()
                for _, member in group:
                    member['userExcluded'] = excluded
                try:
                    _atomic_bytes(root, 'feedback.json', encoded)
                    save_manifest(root, manifest)
                except Exception:
                    # Both authoritative snapshots are restored. A retry remains idempotent.
                    try:
                        if previous_feedback is None:
                            safe_data_path(root, 'feedback.json').unlink(missing_ok=True)
                        else:
                            _atomic_bytes(root, 'feedback.json', previous_feedback)
                        _atomic_bytes(root, 'manifest.json', previous_manifest)
                    except (OSError, MediaError):
                        pass  # The safe error asks the user to reload and retry to repair a partial write.
                    raise FeedbackError() from None
            return {'postId': post_id, 'assetId': asset_id, 'excluded': excluded,
                    'negativeExampleCount': _negative_count(body, groups), 'revision': body['revision']}
    except FeedbackError:
        raise
    except (MediaError, OSError, ValueError, TypeError):
        raise FeedbackError() from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='保存本機家長照片標記，保留原始照片')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--post-id', required=True)
    parser.add_argument('--asset-id', required=True)
    parser.add_argument('--excluded', choices=['1', '0'], required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(set_feedback(args.root, args.post_id, args.asset_id, args.excluded == '1'), ensure_ascii=False))
        return 0
    except FeedbackError as error:
        print(json.dumps({'errorCode': error.reason, 'statusCode': error.status_code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
