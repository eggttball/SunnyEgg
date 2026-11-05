#!/usr/bin/env python3
"""Import a parent-selected Facebook photo and apply the configured photo like."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import parse_qsl, urlsplit

from common import MediaError, SOURCE_PAGE, load_manifest, manifest_lock, safe_data_path, save_manifest
import daily
import facebook
import feedback
import media

PHOTO_ID = re.compile(r'[0-9]+')
POST_FIELDS = 'id,message,created_time,permalink_url,from'
BLOCKED_REASONS = {'batch_locked', 'configuration', 'credentials', 'permission', 'rate_limit',
                   'wrong_page', 'invalid_response', 'photo_unavailable', 'download_failure',
                   'unsupported_media', 'remote_failure'}


def _parent_post(photo_id, token, version, page):
    bridge = daily._graph_object(photo_id, 'id,page_story_id', token, version)
    parent = bridge.get('page_story_id')
    if bridge.get('id') != photo_id or not isinstance(parent, str) or not re.fullmatch(r'[0-9]+_[0-9]+', parent):
        raise daily.DailyError('invalid_response')
    retrieval = parent
    try:
        post = daily._graph_object(parent, POST_FIELDS, token, version)
    except facebook.FetchError as error:
        alias_page = daily.VERIFIED_POST_PAGE_ALIASES.get(page)
        source_page_id = dict(parse_qsl(urlsplit(SOURCE_PAGE).query)).get('id')
        if (error.reason != 'permission' or page != facebook.DEFAULT_PAGE_ID or parent.split('_', 1)[0] != page
                or alias_page is None or alias_page != source_page_id):
            raise
        retrieval = alias_page + '_' + parent.split('_', 1)[1]
        post = daily._graph_object(retrieval, POST_FIELDS, token, version)
    author = post.get('from')
    if parent.split('_', 1)[0] != page or not isinstance(author, dict) or author.get('id') != page:
        raise daily.DailyError('wrong_page')
    if post.get('id') != parent:
        raise daily.DailyError('invalid_response')
    source_url = daily._source_url(post.get('permalink_url'), reject_secrets=True)
    created = post.get('created_time')
    try:
        published_at = datetime.fromisoformat(re.sub(r'([+-][0-9]{2})([0-9]{2})$', r'\1:\2', created.replace('Z', '+0000'))).isoformat()
        if datetime.fromisoformat(published_at).tzinfo is None:
            published_at = None
    except (ValueError, TypeError, AttributeError, OverflowError):
        published_at = None
    title = post.get('message') if isinstance(post.get('message'), str) and post['message'].strip() else '照片貼文'
    return {'sourceId': parent, 'retrievalPostId': retrieval, 'sourceUrl': source_url,
            'publishedAt': published_at, 'title': title}


def _photo_occurrences(manifest, photo_id):
    matches = []
    for post in manifest['posts']:
        if not isinstance(post, dict) or post.get('kind') != 'photos':
            continue
        for asset in post.get('assets', []):
            if isinstance(asset, dict) and feedback.source_photo_id(post, asset) == photo_id:
                matches.append((post, asset))
    return matches


def _selection_action(root, photo_id):
    with manifest_lock(root):
        manifest = load_manifest(root)
        matches = _photo_occurrences(manifest, photo_id)
        if not matches:
            return None
        if any(asset.get('userExcluded') is True for _, asset in matches):
            return 'excluded'
        if all(asset.get('manualSelection') is True for _, asset in matches):
            return 'already_selected'
        for _, asset in matches:
            asset['manualSelection'] = True
        save_manifest(root, manifest)
        return 'promoted'


def _install_photo(root, photo_path, photo_id, item):
    extension = photo_path.suffix.lower()
    try:
        with photo_path.open('rb') as handle:
            media.validate_signature(handle, extension, 'photos')
    except (OSError, MediaError):
        raise daily.DailyError('unsupported_media') from None

    identity = 'facebook:' + item['sourceId']
    directory = safe_data_path(root, 'media')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    installed = None
    created_directory = None
    temporary = None
    try:
        with manifest_lock(root):
            manifest = load_manifest(root)
            matches = _photo_occurrences(manifest, photo_id)
            if matches:
                if any(asset.get('userExcluded') is True for _, asset in matches):
                    return 'excluded'
                if all(asset.get('manualSelection') is True for _, asset in matches):
                    return 'already_selected'
                for _, asset in matches:
                    asset['manualSelection'] = True
                save_manifest(root, manifest)
                return 'promoted'

            post = next((entry for entry in manifest['posts'] if entry.get('kind') == 'photos' and
                         (entry.get('sourceIdentity') == identity or entry.get('sourceUrl') == item['sourceUrl'])), None)
            new_post = post is None
            if new_post:
                post_id = media.generated_id('post')
                post_directory = safe_data_path(root, 'media/' + post_id, allowed={'media'})
                post_directory.mkdir(mode=0o700)
                created_directory = post_directory
                asset_id = media.generated_id('asset')
                name = '01' + extension
                asset_path = post_directory / name
                asset_relative = 'media/{}/{}'.format(post_id, name)
            else:
                post_id = post.get('id')
                if not isinstance(post_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', post_id):
                    raise daily.DailyError('local_storage')
                post_directory = safe_data_path(root, 'media/' + post_id, allowed={'media'})
                if not post_directory.is_dir() or not isinstance(post.get('assets'), list):
                    raise daily.DailyError('local_storage')
                asset_id = media.generated_id('asset')
                name = asset_id + extension
                asset_path = post_directory / name
                asset_relative = 'media/{}/{}'.format(post_id, name)

            descriptor, temporary_name = tempfile.mkstemp(prefix='.manual-photo-', dir=str(post_directory))
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, 'wb') as output, photo_path.open('rb') as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, asset_path)
            temporary = None
            installed = asset_path

            asset = {'id': asset_id, 'type': 'image', 'path': asset_relative, 'name': name,
                     'sourcePhotoId': photo_id, 'manualSelection': True}
            if new_post:
                post = {'id': post_id, 'kind': 'photos', 'sourceUrl': item['sourceUrl'],
                        'sourceIdentity': identity, 'title': item['title'], 'publishedAt': item['publishedAt'],
                        'importedAt': datetime.now(timezone.utc).isoformat(), 'assets': [asset],
                        'recognition': {'state': 'manual', 'message': '由家長手動加入，未執行辨識。', 'results': []},
                        'clips': [], 'dailyPhotoIds': [photo_id]}
                manifest['posts'].append(post)
            else:
                old_assets = post.get('assets') if isinstance(post.get('assets'), list) else []
                old_ids = post.get('dailyPhotoIds')
                if isinstance(old_ids, list) and len(old_ids) == len(old_assets):
                    post['dailyPhotoIds'] = old_ids + [photo_id]
                post['assets'].append(asset)
            save_manifest(root, manifest)
            return 'added'
    except Exception:
        if installed is not None:
            installed.unlink(missing_ok=True)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if created_directory is not None:
            shutil.rmtree(created_directory, ignore_errors=True)
        raise


def _finish_selection(root, profile, auto_like, photo_id, action):
    result = {'state': 'complete', 'action': action, 'reaction': 'not_requested'}
    if action == 'excluded' or not auto_like:
        return result
    try:
        if _selection_action(root, photo_id) == 'excluded':
            result['action'] = 'excluded'
            return result
        state = daily._load_state(root)
        result['reaction'] = daily._react_photo_once(
            root, profile, photo_id, state, datetime.now(timezone.utc).isoformat())
    except Exception as error:
        reason = 'local_storage' if isinstance(error, daily.DailyError) and error.reason == 'local_storage' else 'reaction_failure'
        result.update(state='partial', reaction='failed', reason=reason)
    return result


def import_photo(root, env_file, photo_id):
    if not isinstance(photo_id, str) or PHOTO_ID.fullmatch(photo_id) is None:
        raise daily.DailyError('invalid_response')
    with daily._batch_lock(root):
        try:
            token, version, page, profile, auto_like, _ = daily._configuration(root, env_file)
        except facebook.FetchError:
            raise
        item = _parent_post(photo_id, token, version, page)
        action = _selection_action(root, photo_id)
        if action is None:
            source = daily._photo_source(photo_id, token, version)
            downloads = safe_data_path(root, 'downloads')
            downloads.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.manual-photo-', dir=str(downloads)) as temporary:
                downloaded = facebook.download_native(source, Path(temporary), 1, 'photos')
                action = _install_photo(root, downloaded, photo_id, item)
        return _finish_selection(root, profile, auto_like, photo_id, action)


def main():
    parser = argparse.ArgumentParser(description='Import one explicit Facebook photo and apply the configured like')
    parser.add_argument('--root', required=True)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--photo-id', required=True)
    args = parser.parse_args()
    try:
        result = import_photo(Path(args.root).resolve(), Path(args.env_file), args.photo_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['state'] == 'complete' else 2
    except daily.DailyError as error:
        print(json.dumps({'state': 'blocked', 'reason': error.reason}, ensure_ascii=False))
        return 2
    except facebook.FetchError as error:
        reason = error.reason if error.reason in BLOCKED_REASONS else 'remote_failure'
        print(json.dumps({'state': 'blocked', 'reason': reason}, ensure_ascii=False))
        return 2
    except (MediaError, OSError, ValueError, TypeError):
        print(json.dumps({'state': 'failed', 'reason': 'local_storage'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
