import fcntl
import hashlib
import importlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import facebook
import feedback
import media
import recognize

JPEG = b'\xff\xd8\xff\xe0' + b'photo'
SOURCE = 'https://www.facebook.com/permalink.php?story_fbid=900&id=42'


class Response:
    def __init__(self, value, content_type='application/json'):
        self.payload = json.dumps(value).encode() if isinstance(value, dict) else value
        self.headers = {'Content-Type': content_type}

    def read(self, size=-1):
        result, self.payload = self.payload[:size] if size >= 0 else self.payload, self.payload[size:] if size >= 0 else b''
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Engine:
    def __init__(self, models):
        pass

    def describe_file(self, path):
        return self.describe_bytes(Path(path).read_bytes())

    def describe_bytes(self, content):
        if b'no-face' in content:
            return []
        return [[1.0, 0.0]] if b'candidate' in content or b'reference' in content else [[0.0, 1.0]]


class DailyTests(unittest.TestCase):
    def setUp(self):
        try:
            self.daily = importlib.import_module('daily')
        except ModuleNotFoundError:
            self.fail('daily photo pipeline has not been implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'data/reference-child').mkdir(parents=True)
        (self.root / 'data/reference-child/private-child-name.jpg').write_bytes(JPEG + b'reference')
        (self.root / 'data/models').mkdir()
        for name in recognize.MODEL_NAMES:
            (self.root / 'data/models' / name).write_bytes(b'model' * 1024)
        (self.root / 'config').mkdir()
        (self.root / 'config/job.json').write_text(json.dumps({'version': 1, 'browserProfile': 'data/browser-profile', 'autoLike': True}))
        self.discovery = self.root / 'discovery.json'
        self.write_discovery([{'kind': 'photos', 'photoId': '100', 'sourceUrl': SOURCE}])
        self.environment = mock.patch.dict(os.environ, {'FB_ACCESS_TOKEN': 'DO-NOT-LOG-TOKEN', 'FB_PAGE_ID': '42', 'FB_GRAPH_VERSION': 'v24.0'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.engine = mock.patch.object(recognize, 'FaceEngine', Engine)
        self.engine.start()
        self.addCleanup(self.engine.stop)
        self.requests = []
        self.contents = {'100': b'negative', '102': b'no-face'}
        self.failed_posts = set()
        self.wrong_page = False
        self.attachment_next = False
        self.mixed = False
        self.published_at = '2026-10-30T01:00:00+0000'

    def write_discovery(self, posts, state='complete'):
        self.discovery.write_text(json.dumps({'version': 1, 'capturedAt': '2026-11-01T07:00:00+00:00', 'state': state, 'posts': posts, 'skippedVideos': 0}))

    def post(self, identifier='42_900'):
        leaves = [{'type': 'photo', 'target': {'id': photo_id},
                   'media': {'image': {'src': 'https://scontent.fbcdn.net/{}-large.jpg?signature=DO-NOT-LOG-SIGNATURE'.format(photo_id),
                                       'width': 200, 'height': 100}}} for photo_id in self.contents]
        if identifier == '42_901':
            leaves[0]['target']['id'] = '200'
        if self.mixed:
            leaves.append({'type': 'video_inline', 'target': {'id': '333'}})
        children = {'data': leaves[:1] if self.attachment_next else leaves}
        if self.attachment_next:
            children['paging'] = {'next': 'https://graph.facebook.com/v24.0/album/photos?after=NEXT&access_token=REMOTE-TOKEN'}
        return {'id': identifier, 'from': {'id': '99' if self.wrong_page else '42'}, 'message': 'Class photos',
                'created_time': self.published_at, 'permalink_url': SOURCE,
                'attachments': {'data': [{'type': 'album', 'subattachments': children}]}}

    def http(self, request, timeout=30):
        self.requests.append(request)
        parts = urlsplit(request.full_url)
        query = parse_qs(parts.query)
        if parts.hostname == 'graph.facebook.com':
            self.assertFalse('access_token' in query)
            self.assertEqual(request.get_header('Authorization'), 'Bearer DO-NOT-LOG-TOKEN')
            if parts.path == '/v24.0/album/photos':
                return Response({'data': [{'type': 'photo', 'target': {'id': '102'},
                                           'media': {'image': {'src': 'https://scontent.fbcdn.net/102-large.jpg?signature=DO-NOT-LOG-SIGNATURE',
                                                               'width': 200, 'height': 100}}}]})
            object_id = parts.path.rsplit('/', 1)[1]
            fields = query.get('fields', [''])[0]
            if fields == 'id,page_story_id':
                return Response({'id': object_id, 'page_story_id': '42_' + ('901' if object_id == '200' else '900')})
            if object_id.startswith('42_'):
                if object_id in self.failed_posts:
                    return Response({'error': {'code': 200, 'message': 'DO-NOT-LOG-TOKEN signed-url private-child-name.jpg'}})
                return Response(self.post(object_id))
            self.assertEqual(fields, 'id,images')
            return Response({'id': object_id, 'images': [
                {'width': 40, 'height': 50, 'source': 'https://scontent.fbcdn.net/{}-small.jpg'.format(object_id)},
                {'width': 200, 'height': 100, 'source': 'https://scontent.fbcdn.net/{}-large.jpg?signature=DO-NOT-LOG-SIGNATURE'.format(object_id)}]})
        self.assertEqual(parts.hostname, 'scontent.fbcdn.net')
        self.assertNotIn('small', parts.path)
        self.assertIsNone(request.get_header('Authorization'))
        identifier = parts.path.rsplit('/', 1)[1].split('-')[0]
        return Response(JPEG + self.contents['100' if identifier == '200' else identifier], 'image/jpeg')

    def run_batch(self, **options):
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            return self.daily_run(self.root, discovery_file=self.discovery, simulate=True, **options)

    def daily_run(self, *args, **options):
        try:
            return self.daily.run_daily(*args, **options)
        except TypeError as error:
            if 'unexpected keyword argument' in str(error):
                self.fail('daily pipeline does not accept the requested date/scan options')
            raise

    def test_negatives_are_private_and_full_album_pagination_is_checked(self):
        self.attachment_next = True
        report = self.run_batch()
        self.assertEqual(report['state'], 'complete')
        self.assertEqual(report['counts']['inspectedPhotos'], 2)
        self.assertEqual(report['counts']['noMatchPhotos'], 1)
        self.assertEqual(report['counts']['noFacePhotos'], 1)
        self.assertEqual(common.load_manifest(self.root)['posts'], [])
        self.assertEqual(list((self.root / 'data/downloads').iterdir()), [])
        self.assertEqual(list((self.root / 'data').glob('media/**/*.jpg')), [])
        self.assertEqual(common.load_manifest(self.root)['jobStatus'], report)
        reports = list((self.root / 'data/runs').glob('*.json'))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text()), report)
        self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)
        encoded = json.dumps(report)
        for private in ('DO-NOT-LOG', 'private-child-name', 'scontent', 'signature', 'reference-child'):
            self.assertNotIn(private, encoded)

    def test_explicit_subattachment_limit_inspects_entire_large_album(self):
        self.contents = {str(identifier): b'negative' for identifier in range(100, 151)}
        original = self.http
        def limited_http(request, timeout=30):
            query = parse_qs(urlsplit(request.full_url).query)
            if 'attachments' in query.get('fields', [''])[0]:
                post = self.post()
                if 'subattachments.limit(100)' not in query['fields'][0]:
                    post['attachments']['data'][0]['subattachments']['data'] = post['attachments']['data'][0]['subattachments']['data'][:12]
                return Response(post)
            return original(request, timeout)
        with mock.patch.object(facebook, 'urlopen', side_effect=limited_http):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
        self.assertEqual(report['counts']['inspectedPhotos'], 51)
        self.assertEqual(report['posts'][0]['attachments']['photoCount'], 51)

    def test_resolved_attachment_media_avoids_redundant_photo_object_lookups(self):
        report = self.run_batch()
        image_queries = [request for request in self.requests if parse_qs(urlsplit(request.full_url).query).get('fields') == ['id,images']]
        self.assertEqual(image_queries, [])
        self.assertEqual(report['counts']['inspectedPhotos'], 2)
        self.assertEqual(report['posts'][0]['photoSourceMode'], 'attachment_media')
        self.assertEqual(report['posts'][0]['photoSourceQuality'], 'attachment_provided')
        self.assertEqual(report['posts'][0]['photoObjectLookups'], 0)
        encoded = json.dumps(report) + (self.root / 'data/daily-state.json').read_text()
        self.assertNotIn('DO-NOT-LOG-SIGNATURE', encoded)
        self.assertNotIn('scontent.fbcdn.net', encoded)

    def test_metadata_hash_excludes_rotating_sources_and_tracks_photo_ids(self):
        hasher = getattr(self.daily, '_post_metadata_hash', None)
        self.assertTrue(callable(hasher))
        base = {'sourceId': '42_900', 'photoIds': ['100', '102'],
                'photoSources': {'100': 'https://scontent.fbcdn.net/100.jpg?signature=private-first'}}
        rotated = {**base, 'photoSources': {'100': 'https://scontent.fbcdn.net/100.jpg?signature=private-second'}}
        expanded = {**base, 'photoIds': ['100', '102', '103']}
        self.assertEqual(hasher(base), hasher(rotated))
        self.assertNotEqual(hasher(base), hasher(expanded))

    def test_rotated_attachment_sources_reuse_complete_check_cache(self):
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            self.daily.run_daily(self.root, discovery_file=self.discovery)
            post = self.post()
            post['attachments']['data'][0]['subattachments']['data'][0]['media']['image']['src'] += '-rotated'
            with mock.patch.object(self, 'post', return_value=post):
                report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['counts']['cachedPosts'], 1)
        self.assertEqual(report['counts']['inspectedPhotos'], 0)

    def test_feedback_invalidates_check_cache_and_undo_restores_candidate(self):
        self.contents['100'] = b'candidate'
        (self.root / 'config/job.json').write_text(json.dumps({'version': 1, 'browserProfile': 'data/browser-profile', 'autoLike': False}))
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            first = self.daily.run_daily(self.root, discovery_file=self.discovery)
            manifest = common.load_manifest(self.root)
            asset = manifest['posts'][0]['assets'][0]
            self.assertEqual(asset.get('sourcePhotoId'), '100')
            asset['userExcluded'] = True
            common.save_manifest(self.root, manifest)
            content = (self.root / 'data' / asset['path']).read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            relative = 'negative-examples/' + digest + '.jpg'
            (self.root / 'data/negative-examples').mkdir()
            (self.root / 'data' / relative).write_bytes(content)
            feedback = {'version': 1, 'revision': 1, 'examples': {asset['id']: {
                'postId': manifest['posts'][0]['id'], 'assetId': asset['id'], 'excluded': True,
                'imageHash': digest, 'path': relative, 'photoId': '100', 'updatedAt': '2026-11-01T12:00:00+00:00'}}}
            feedback_path = self.root / 'data/feedback.json'
            feedback_path.write_text(json.dumps(feedback))
            self.contents['100'] = b'candidate-other-resolution'
            excluded = self.daily.run_daily(self.root, discovery_file=self.discovery)
            cached = self.daily.run_daily(self.root, discovery_file=self.discovery)
            feedback['examples'][asset['id']]['excluded'] = False
            feedback_path.write_text(json.dumps(feedback))
            manifest = common.load_manifest(self.root)
            manifest['posts'][0]['assets'][0]['userExcluded'] = False
            common.save_manifest(self.root, manifest)
            restored = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(first['counts']['candidatePhotos'], 1)
        self.assertEqual(excluded['counts']['cachedPosts'], 0)
        self.assertEqual(excluded['counts']['inspectedPhotos'], 2)
        self.assertEqual(excluded['counts']['candidatePhotos'], 0)
        result = excluded['posts'][0]['results'][0]
        self.assertEqual(result['reason'], 'negative_example')
        self.assertEqual(result['negativeExampleCount'], 1)
        self.assertEqual(cached['counts']['cachedPosts'], 1)
        self.assertEqual(restored['counts']['cachedPosts'], 0)
        self.assertEqual(restored['counts']['candidatePhotos'], 1)
        encoded = json.dumps(excluded)
        self.assertNotIn('features', encoded)
        self.assertNotIn('negative-examples/', encoded)
        self.assertNotIn(digest, encoded)

    def prepare_unidentified_negative(self):
        legacy = self.import_legacy_photos([b'negative'])
        feedback.set_feedback(self.root, legacy['id'], legacy['assets'][0]['id'], True)
        return legacy

    def test_excluded_unknown_identity_backfill_rejects_reencoding_without_liking(self):
        legacy = self.prepare_unidentified_negative()
        before = feedback.load_feedback(self.root)
        requests = []
        def browser_action(*args):
            requests.append(args[1])
            return {'state': 'complete', 'reaction': 'liked'}
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily, '_browser', side_effect=browser_action):
            learned = self.daily.run_daily(self.root, discovery_file=self.discovery)
            self.contents['100'] = b'candidate-reencoded'
            reencoded = self.daily.run_daily(self.root, discovery_file=self.discovery, force_recheck=True)
        history = feedback.load_feedback(self.root)['examples'][legacy['assets'][0]['id']]
        self.assertEqual(learned['state'], 'complete')
        self.assertEqual(history.get('photoId'), '100')
        self.assertEqual(history['imageHash'], before['examples'][legacy['assets'][0]['id']]['imageHash'])
        self.assertTrue(history['excluded'])
        self.assertEqual(reencoded['counts']['candidatePhotos'], 0)
        self.assertEqual(reencoded['counts']['likedPhotos'], 0)
        self.assertEqual(reencoded['posts'][0]['results'][0]['reason'], 'negative_example')
        self.assertEqual(requests, [])
        self.assertTrue(common.load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])

    def test_already_known_legacy_negative_ids_upgrade_before_reencoded_classification(self):
        legacy = self.prepare_unidentified_negative()
        for field in ('sourcePhotoId', 'dailyPhotoIds'):
            with self.subTest(field=field):
                manifest = common.load_manifest(self.root)
                post = manifest['posts'][0]
                post.pop('dailyPhotoIds', None)
                post['assets'][0].pop('sourcePhotoId', None)
                if field == 'sourcePhotoId':
                    post['assets'][0][field] = '100'
                else:
                    post[field] = ['100']
                common.save_manifest(self.root, manifest)
                body = feedback.load_feedback(self.root)
                body['examples'][legacy['assets'][0]['id']].pop('photoId', None)
                (self.root / 'data/feedback.json').write_text(json.dumps(body))
                self.contents['100'] = b'candidate-reencoded'
                requests = []
                def browser_action(*args):
                    requests.append(args[1])
                    return {'state': 'complete', 'reaction': 'liked'}
                with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                        mock.patch.object(self.daily, '_browser', side_effect=browser_action):
                    report = self.daily.run_daily(self.root, discovery_file=self.discovery, force_recheck=True)
                self.assertEqual(report['state'], 'complete')
                self.assertEqual(report['counts']['candidatePhotos'], 0)
                self.assertEqual(requests, [])
                self.assertEqual(feedback.load_feedback(self.root)['examples'][legacy['assets'][0]['id']].get('photoId'), '100')

    def test_backfilled_negative_identity_refreshes_remaining_batch_cache(self):
        self.prepare_unidentified_negative()
        active_parent = {'id': None}
        def post(identifier='42_900'):
            active_parent['id'] = identifier
            ids = ['100'] if identifier == '42_900' else ['100', '200']
            leaves = [{'type': 'photo', 'target': {'id': photo_id},
                       'media': {'image': {'src': 'https://scontent.fbcdn.net/{}-large.jpg'.format(photo_id),
                                           'width': 200, 'height': 100}}} for photo_id in ids]
            return {'id': identifier, 'from': {'id': '42'}, 'message': 'Class photos',
                    'created_time': self.published_at,
                    'permalink_url': 'https://www.facebook.com/permalink.php?story_fbid={}&id=42'.format(identifier.split('_')[1]),
                    'attachments': {'data': [{'type': 'album', 'subattachments': {'data': leaves}}]}}
        original_http = self.http
        def http(request, timeout=30):
            parts = urlsplit(request.full_url)
            if parts.hostname == 'scontent.fbcdn.net':
                identifier = parts.path.rsplit('/', 1)[1].split('-')[0]
                content = b'no-face' if identifier == '200' else b'negative' if active_parent['id'] == '42_900' else b'candidate-reencoded'
                return Response(JPEG + content, 'image/jpeg')
            return original_http(request, timeout)
        requests = []
        def browser_action(*args):
            requests.append(args[1])
            return {'state': 'complete', 'reaction': 'liked'}
        self.write_discovery([{'kind': 'photos', 'photoId': '200', 'sourceUrl': SOURCE}])
        with mock.patch.object(self, 'post', side_effect=post), mock.patch.object(facebook, 'urlopen', side_effect=http), \
                mock.patch.object(self.daily, '_browser', side_effect=browser_action):
            cached_candidate = self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
            self.write_discovery([{'kind': 'photos', 'photoId': photo_id, 'sourceUrl': SOURCE} for photo_id in ('100', '200')])
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(cached_candidate['counts']['candidatePhotos'], 1)
        self.assertEqual(report['state'], 'complete')
        self.assertEqual(report['counts']['cachedPosts'], 0)
        self.assertEqual(report['counts']['inspectedPhotos'], 3)
        self.assertEqual(report['counts']['candidatePhotos'], 0)
        self.assertEqual([post['checkMode'] for post in report['posts']], ['fresh', 'fresh'])
        self.assertEqual(requests, [])

    def test_external_undo_after_identity_commit_stops_expected_profile_claims(self):
        legacy = self.prepare_unidentified_negative()
        original_commit = feedback.commit_identity_backfills
        changed = {'done': False}
        def commit_then_undo(*args, **options):
            body = original_commit(*args, **options)
            if len(args) > 2 and args[2] and not changed['done']:
                changed['done'] = True
                current = feedback.load_feedback(self.root)
                current['revision'] += 1
                current['examples'][legacy['assets'][0]['id']]['excluded'] = False
                (self.root / 'data/feedback.json').write_text(json.dumps(current))
                manifest = common.load_manifest(self.root)
                manifest['posts'][0]['assets'][0]['userExcluded'] = False
                common.save_manifest(self.root, manifest)
            return body
        requests = []
        def browser_action(*args):
            requests.append(args[1])
            return {'state': 'complete', 'reaction': 'liked'}
        with mock.patch.object(feedback, 'commit_identity_backfills', side_effect=commit_then_undo), \
                mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily, '_browser', side_effect=browser_action):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'feedback_changed')
        self.assertEqual(report['posts'], [])
        self.assertEqual(requests, [])
        self.assertFalse(feedback.load_feedback(self.root)['examples'][legacy['assets'][0]['id']]['excluded'])
        self.assertFalse(common.load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])

    def test_failed_paired_identity_commit_does_not_adopt_profile_or_identifiers(self):
        legacy = self.prepare_unidentified_negative()
        before = feedback.load_feedback(self.root)
        instances = []
        class TrackedEngine(Engine):
            def __init__(self, models):
                instances.append(self)
        original_save = feedback.save_manifest
        def fail_backfill(root, manifest):
            if any(asset.get('sourcePhotoId') == '100' for post in manifest['posts'] for asset in post.get('assets', [])):
                raise OSError('synthetic paired write failure')
            return original_save(root, manifest)
        requests = []
        def browser_action(*args):
            requests.append(args[1])
            return {'state': 'complete', 'reaction': 'liked'}
        with mock.patch.object(recognize, 'FaceEngine', TrackedEngine), \
                mock.patch.object(feedback, 'save_manifest', side_effect=fail_backfill), \
                mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily, '_browser', side_effect=browser_action):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(requests, [])
        self.assertEqual(feedback.load_feedback(self.root), before)
        self.assertNotIn('sourcePhotoId', common.load_manifest(self.root)['posts'][0]['assets'][0])
        self.assertEqual(instances[0].negative_profile['photoIds'], set())
        self.assertEqual(instances[0].negative_profile['fingerprint'], recognize.feedback_fingerprint(self.root))
        self.assertEqual(len(common.load_manifest(self.root)['posts'][0]['assets']), len(legacy['assets']))

    def test_corrupt_feedback_blocks_before_remote_discovery_or_photo_requests(self):
        (self.root / 'data/feedback.json').write_text('{invalid-private-feedback')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'feedback_unavailable')
        self.assertEqual(self.requests, [])
        self.assertNotIn('invalid-private-feedback', json.dumps(report))

    def label_existing_candidate(self):
        manifest = common.load_manifest(self.root)
        post = manifest['posts'][0]
        asset = post['assets'][0]
        asset['userExcluded'] = True
        common.save_manifest(self.root, manifest)
        content = (self.root / 'data' / asset['path']).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        directory = self.root / 'data/negative-examples'
        directory.mkdir(exist_ok=True)
        (directory / (digest + '.jpg')).write_bytes(content)
        body = {'version': 1, 'revision': 1, 'examples': {asset['id']: {
            'postId': post['id'], 'assetId': asset['id'], 'excluded': True,
            'imageHash': digest, 'path': 'negative-examples/' + digest + '.jpg',
            'photoId': '100', 'updatedAt': '2026-11-01T12:00:00+00:00'}}}
        (self.root / 'data/feedback.json').write_text(json.dumps(body))

    def test_feedback_during_download_stops_batch_without_promotion_or_reaction(self):
        self.contents['100'] = b'candidate'
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
            before_state = (self.root / 'data/daily-state.json').read_bytes()
            self.write_discovery([{'kind': 'photos', 'photoId': identifier, 'sourceUrl': SOURCE}
                                  for identifier in ('100', '200', '300')])
            original_download = facebook.download_native
            def download_then_label(*args):
                path = original_download(*args)
                if args[2] == 1:
                    self.label_existing_candidate()
                return path
            with mock.patch.object(facebook, 'download_native', side_effect=download_then_label), \
                    mock.patch.object(self.daily, '_browser') as browser:
                report = self.daily.run_daily(self.root, discovery_file=self.discovery, force_recheck=True)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'feedback_changed')
        self.assertEqual(report['counts']['unprocessedObservations'], 2)
        self.assertEqual(report['counts']['processedPosts'], 0)
        self.assertEqual(report['posts'], [])
        browser.assert_not_called()
        self.assertEqual((self.root / 'data/daily-state.json').read_bytes(), before_state)
        self.assertEqual(len(common.load_manifest(self.root)['posts'][0]['assets']), 1)
        self.assertTrue(common.load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])

    def test_feedback_after_cache_validation_blocks_cached_reaction_and_claim(self):
        self.contents['100'] = b'candidate'
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
            before_state = (self.root / 'data/daily-state.json').read_bytes()
            original_fingerprint = self.daily._gallery_cache_fingerprint
            def fingerprint_then_label(*args):
                result = original_fingerprint(*args)
                self.label_existing_candidate()
                return result
            with mock.patch.object(self.daily, '_gallery_cache_fingerprint', side_effect=fingerprint_then_label), \
                    mock.patch.object(self.daily, '_browser') as browser:
                report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'feedback_changed')
        self.assertEqual(report['counts']['cachedPosts'], 0)
        self.assertEqual(report['posts'], [])
        browser.assert_not_called()
        self.assertEqual((self.root / 'data/daily-state.json').read_bytes(), before_state)

    def test_feedback_immediately_before_new_post_import_installs_nothing(self):
        self.contents['100'] = b'candidate'
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
            before_state = (self.root / 'data/daily-state.json').read_bytes()
            before_files = sorted(path.relative_to(self.root).as_posix() for path in (self.root / 'data/media').glob('**/*') if path.is_file())
            self.write_discovery([{'kind': 'photos', 'photoId': '200', 'sourceUrl': SOURCE}])
            original_post = self.post
            def distinct_post(post_id='42_900'):
                post = original_post(post_id)
                if post_id == '42_901':
                    post['permalink_url'] = 'https://www.facebook.com/permalink.php?story_fbid=901&id=42'
                return post
            original_import = media.import_media
            def import_after_label(*args, **kwargs):
                self.label_existing_candidate()
                return original_import(*args, **kwargs)
            with mock.patch.object(self, 'post', side_effect=distinct_post), \
                    mock.patch.object(media, 'import_media', side_effect=import_after_label), \
                    mock.patch.object(self.daily, '_browser') as browser:
                report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['reason'], 'feedback_changed')
        self.assertEqual(report['counts']['processedPosts'], 0)
        self.assertEqual(len(common.load_manifest(self.root)['posts']), 1)
        self.assertEqual(sorted(path.relative_to(self.root).as_posix() for path in (self.root / 'data/media').glob('**/*') if path.is_file()), before_files)
        self.assertEqual((self.root / 'data/daily-state.json').read_bytes(), before_state)
        browser.assert_not_called()

    def import_legacy_photos(self, contents):
        files = []
        for index, content in enumerate(contents):
            path = self.root / ('legacy-{}.jpg'.format(index))
            path.write_bytes(JPEG + content)
            files.append(path)
        return media.import_media(self.root, 'photos', SOURCE, files, source_id='facebook:42_900')

    def test_legacy_album_append_then_reencoded_same_photo_does_not_add_again(self):
        legacy = self.import_legacy_photos([b'negative', b'no-face'])
        original_ids = [asset['id'] for asset in legacy['assets']]
        self.contents['103'] = b'candidate-first-encoding'
        first = self.run_batch()
        after_first = common.load_manifest(self.root)['posts'][0]
        self.assertEqual(len(after_first['assets']), 3)
        promoted_id = after_first['assets'][-1]['id']
        self.assertEqual(after_first['assets'][-1]['sourcePhotoId'], '103')
        self.contents['103'] = b'candidate-second-encoding'
        second = self.run_batch()
        current = common.load_manifest(self.root)['posts'][0]
        self.assertEqual(len(current['assets']), 3)
        self.assertEqual([asset['id'] for asset in current['assets'][:2]], original_ids)
        self.assertEqual(current['assets'][-1]['id'], promoted_id)
        self.assertEqual(second['posts'][0]['retainedPhotoCount'], first['posts'][0]['retainedPhotoCount'])

    def test_primary_and_mixed_legacy_identifiers_all_prevent_repeat_imports(self):
        legacy = self.import_legacy_photos([b'candidate-old-100', b'candidate-old-102'])
        manifest = common.load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        manifest['posts'][0]['assets'][0]['userExcluded'] = True
        manifest['posts'][0]['dailyPhotoIds'] = [None, '102', 'invalid', 999, True]
        common.save_manifest(self.root, manifest)
        self.contents = {'100': b'candidate-new-100', '102': b'candidate-new-102'}
        self.run_batch()
        current = common.load_manifest(self.root)['posts'][0]
        self.assertEqual(len(current['assets']), 2)
        self.assertEqual([asset['id'] for asset in current['assets']], [asset['id'] for asset in legacy['assets']])
        self.assertTrue(current['assets'][0]['userExcluded'])
        self.assertEqual(current['assets'][0]['sourcePhotoId'], '100')

    def test_unknown_legacy_bytes_backfill_identity_for_future_reencodings(self):
        legacy = self.import_legacy_photos([b'candidate'])
        self.contents['100'] = b'candidate'
        self.run_batch()
        current = common.load_manifest(self.root)['posts'][0]
        self.assertEqual(len(current['assets']), 1)
        self.assertEqual(current['assets'][0]['id'], legacy['assets'][0]['id'])
        self.assertEqual(current['assets'][0].get('sourcePhotoId'), '100')
        self.contents['100'] = b'candidate-other-resolution'
        self.run_batch()
        self.assertEqual(len(common.load_manifest(self.root)['posts'][0]['assets']), 1)

    def test_distinct_known_photo_ids_are_not_collapsed_by_identical_bytes(self):
        self.contents['100'] = b'candidate'
        self.run_batch()
        manifest = common.load_manifest(self.root)
        manifest['posts'][0].pop('dailyPhotoIds')
        common.save_manifest(self.root, manifest)
        self.contents['103'] = b'candidate'
        self.run_batch()
        current = common.load_manifest(self.root)['posts'][0]
        self.assertEqual(len(current['assets']), 2)
        self.assertEqual({asset['sourcePhotoId'] for asset in current['assets']}, {'100', '103'})

    def test_same_photo_id_under_another_post_keeps_only_original_asset(self):
        self.contents['100'] = b'candidate'
        self.run_batch()
        manifest = common.load_manifest(self.root)
        original = manifest['posts'][0]['assets'][0]
        original['userExcluded'] = True
        common.save_manifest(self.root, manifest)
        path = self.root / 'another-resolution.jpg'
        path.write_bytes(JPEG + b'candidate-other-resolution')
        item = {'sourceId': '42_901', 'sourceUrl': 'https://www.facebook.com/permalink.php?story_fbid=901&id=42',
                'photoIds': ['100'], 'title': 'Another post', 'publishedAt': '2026-10-31T01:00:00+00:00'}
        result = {'state': 'candidate', 'score': 1.0, 'faceCount': 1}
        gallery_id = self.daily._gallery_record(self.root, item, [path], [result], Engine(None), [[1.0, 0.0]],
                                                0.6, '2026-11-01T12:00:00+00:00')
        current = common.load_manifest(self.root)
        self.assertIsNone(gallery_id)
        self.assertEqual(len(current['posts']), 1)
        self.assertEqual(current['posts'][0]['assets'], [original])

    def test_unknown_legacy_byte_match_is_consumed_once_for_distinct_ids(self):
        self.import_legacy_photos([b'candidate'])
        self.contents = {'100': b'candidate', '103': b'candidate'}
        self.run_batch()
        assets = common.load_manifest(self.root)['posts'][0]['assets']
        self.assertEqual(len(assets), 2)
        self.assertEqual({asset.get('sourcePhotoId') for asset in assets}, {'100', '103'})

    def test_cross_post_known_id_backfills_current_unknown_exact_byte_copy(self):
        self.contents['100'] = b'candidate'
        self.run_batch()
        source = 'https://www.facebook.com/permalink.php?story_fbid=901&id=42'
        path = self.root / 'legacy-copy.jpg'
        path.write_bytes(JPEG + b'candidate')
        legacy = media.import_media(self.root, 'photos', source, [path], source_id='facebook:42_901')
        manifest = common.load_manifest(self.root)
        manifest['posts'][1]['assets'][0]['userExcluded'] = True
        common.save_manifest(self.root, manifest)
        item = {'sourceId': '42_901', 'sourceUrl': source, 'photoIds': ['100'],
                'title': 'Another post', 'publishedAt': '2026-10-31T01:00:00+00:00'}
        result = {'state': 'candidate', 'score': 1.0, 'faceCount': 1}
        self.daily._gallery_record(self.root, item, [path], [result], Engine(None), [[1.0, 0.0]],
                                   0.6, '2026-11-01T12:00:00+00:00')
        current = common.load_manifest(self.root)
        copy = current['posts'][1]['assets'][0]
        self.assertEqual(copy.get('sourcePhotoId'), '100')
        self.assertEqual(copy['id'], legacy['assets'][0]['id'])
        self.assertTrue(copy['userExcluded'])
        self.assertEqual([self.daily._retained_photo_count(current, post['id']) for post in current['posts']], [1, 0])
        self.assertEqual(len(list((self.root / 'data/media').glob('**/*.jpg'))), 2)

    def test_locally_known_id_does_not_consume_unknown_same_byte_distinct_photo(self):
        legacy = self.import_legacy_photos([b'candidate', b'candidate'])
        manifest = common.load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        common.save_manifest(self.root, manifest)
        self.contents = {'100': b'candidate', '103': b'candidate'}
        self.run_batch()
        current = common.load_manifest(self.root)['posts'][0]
        self.assertEqual([asset['id'] for asset in current['assets']], [asset['id'] for asset in legacy['assets']])
        self.assertEqual([asset.get('sourcePhotoId') for asset in current['assets']], ['100', '103'])

    def test_retained_count_keeps_ambiguous_repeated_asset_ids_independent(self):
        manifest = {'posts': [{'id': post_id, 'kind': 'photos',
                               'assets': [{'id': 'same', 'type': 'image', 'sourcePhotoId': '100'}]}
                              for post_id in ('post-first', 'post-second')]}
        self.assertEqual([self.daily._retained_photo_count(manifest, post_id)
                          for post_id in ('post-first', 'post-second')], [1, 1])

    def test_retained_count_ignores_globally_conflicting_alias_family(self):
        manifest = {'posts': [
            {'id': 'post-first', 'kind': 'photos',
             'assets': [{'id': 'a', 'type': 'image', 'sourcePhotoId': '100', 'duplicateOf': 'c'}]},
            {'id': 'post-second', 'kind': 'photos',
             'assets': [{'id': 'c', 'type': 'image', 'sourcePhotoId': '100'},
                        {'id': 'b', 'type': 'image', 'sourcePhotoId': '200', 'duplicateOf': 'c'}]}]}
        self.assertEqual([self.daily._retained_photo_count(manifest, post_id)
                          for post_id in ('post-first', 'post-second')], [1, 1])

    def test_cross_post_suppression_cache_tracks_retained_photo_identity(self):
        self.contents['100'] = b'candidate'
        self.run_batch()
        item = {'sourceId': '42_901', 'sourceUrl': 'https://www.facebook.com/permalink.php?story_fbid=901&id=42',
                'photoIds': ['100']}
        before = self.daily._gallery_cache_fingerprint(self.root, item)
        manifest = common.load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '101'
        manifest['posts'][0]['dailyPhotoIds'] = ['101']
        common.save_manifest(self.root, manifest)
        after = self.daily._gallery_cache_fingerprint(self.root, item)
        self.assertNotEqual(before, after)

    def test_unknown_alias_family_consumes_one_byte_match_for_distinct_ids(self):
        legacy = self.import_legacy_photos([b'candidate', b'candidate'])
        manifest = common.load_manifest(self.root)
        duplicate, canonical = manifest['posts'][0]['assets']
        duplicate['duplicateOf'] = canonical['id']
        common.save_manifest(self.root, manifest)
        self.contents = {'100': b'candidate', '103': b'candidate'}
        report = self.run_batch()
        assets = common.load_manifest(self.root)['posts'][0]['assets']
        self.assertEqual(len(assets), 3)
        self.assertEqual([asset['id'] for asset in assets[:2]], [asset['id'] for asset in legacy['assets']])
        self.assertEqual([asset.get('sourcePhotoId') for asset in assets], ['100', '100', '103'])
        self.assertEqual(assets[0]['duplicateOf'], assets[1]['id'])
        self.assertEqual(report['posts'][0]['retainedPhotoCount'], 2)

    def test_retained_count_uses_logical_photos_including_excluded_canonical(self):
        self.import_legacy_photos([b'candidate', b'candidate'])
        manifest = common.load_manifest(self.root)
        duplicate, canonical = manifest['posts'][0]['assets']
        duplicate['duplicateOf'] = canonical['id']
        duplicate['sourcePhotoId'] = canonical['sourcePhotoId'] = '100'
        canonical['userExcluded'] = True
        common.save_manifest(self.root, manifest)
        self.contents['100'] = b'candidate-other-resolution'
        report = self.run_batch()
        self.assertEqual(report['posts'][0]['retainedPhotoCount'], 1)
        self.assertEqual(len(common.load_manifest(self.root)['posts'][0]['assets']), 2)

    def test_missing_attachment_source_uses_existing_largest_image_lookup(self):
        post = self.post()
        post['attachments']['data'][0]['subattachments']['data'][0]['media'] = {}
        with mock.patch.object(self, 'post', return_value=post):
            report = self.run_batch()
        image_queries = [request for request in self.requests if parse_qs(urlsplit(request.full_url).query).get('fields') == ['id,images']]
        self.assertEqual(len(image_queries), 1)
        self.assertEqual(report['counts']['inspectedPhotos'], 2)
        self.assertEqual(report['posts'][0]['photoSourceMode'], 'mixed')
        self.assertEqual(report['posts'][0]['photoObjectLookups'], 1)

    def test_unsafe_attachment_source_blocks_before_download(self):
        post = self.post()
        post['attachments']['data'][0]['subattachments']['data'][0]['media']['image']['src'] = 'https://evil.test/private.jpg'
        with mock.patch.object(self, 'post', return_value=post):
            report = self.run_batch()
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'unsafe_url')
        self.assertEqual(report['counts']['inspectedPhotos'], 0)
        self.assertEqual(len(self.requests), 2)

    def test_rate_limit_preserves_success_and_stops_remaining_observations(self):
        self.write_discovery([{'kind': 'photos', 'photoId': photo_id, 'sourceUrl': SOURCE} for photo_id in ['100', '200', '300']])
        original = self.http
        def limited_http(request, timeout=30):
            identifier = urlsplit(request.full_url).path.rsplit('/', 1)[1]
            if identifier == '42_901':
                self.requests.append(request)
                return Response({'error': {'code': 4, 'is_transient': True, 'message': 'DO-NOT-LOG'}})
            if identifier == '300':
                self.fail('rate-limited run continued to another observation')
            return original(request, timeout)
        with mock.patch.object(facebook, 'urlopen', side_effect=limited_http):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['reason'], 'rate_limit')
        self.assertEqual(report['counts']['processedPosts'], 1)
        self.assertEqual(report['counts']['failedPosts'], 1)
        self.assertEqual(report['counts']['skippedPosts'], 0)
        self.assertEqual(report['counts']['unprocessedObservations'], 1)
        self.assertEqual(report['counts']['inspectedPhotos'], 2)
        self.assertEqual(len(report['failures']), 1)
        self.assertNotIn('DO-NOT-LOG', json.dumps(report))

    def test_rate_limit_before_any_success_reports_blocked_without_retries(self):
        self.write_discovery([{'kind': 'photos', 'photoId': photo_id, 'sourceUrl': SOURCE} for photo_id in ['100', '200']])
        with mock.patch.object(facebook, 'urlopen', return_value=Response({'error': {'code': 17}})) as boundary:
            report = self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'rate_limit')
        self.assertEqual(report['counts']['failedPosts'], 1)
        self.assertEqual(report['counts']['unprocessedObservations'], 1)
        self.assertEqual(report['counts']['skippedPosts'], 0)
        self.assertEqual(boundary.call_count, 1)

    def test_http_429_with_generic_graph_error_stops_the_batch(self):
        self.write_discovery([{'kind': 'photos', 'photoId': photo_id, 'sourceUrl': SOURCE} for photo_id in ['100', '200', '300']])
        original = self.http
        def limited_http(request, timeout=30):
            identifier = urlsplit(request.full_url).path.rsplit('/', 1)[1]
            if identifier == '42_901':
                self.requests.append(request)
                raise HTTPError(request.full_url, 429, 'rate limited', {}, io.BytesIO(b'{"error":{"code":1}}'))
            if identifier == '300':
                self.fail('HTTP 429 did not stop further observations')
            return original(request, timeout)
        with mock.patch.object(facebook, 'urlopen', side_effect=limited_http):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['reason'], 'rate_limit')
        self.assertEqual(report['counts']['processedPosts'], 1)
        self.assertEqual(report['counts']['failedPosts'], 1)
        self.assertEqual(report['counts']['unprocessedObservations'], 1)
        self.assertEqual(len(report['failures']), 1)

    def test_terminal_rate_limit_reason_takes_precedence_over_earlier_failure(self):
        self.write_discovery([{'kind': 'photos', 'photoId': photo_id, 'sourceUrl': SOURCE} for photo_id in ['100', '200', '300']])
        def failing_http(request, timeout=30):
            self.requests.append(request)
            identifier = urlsplit(request.full_url).path.rsplit('/', 1)[1]
            if identifier == '100':
                return Response({'error': {'code': 200}})
            if identifier == '200':
                return Response({'error': {'code': 17}})
            self.fail('terminal rate limit did not stop the batch')
        with mock.patch.object(facebook, 'urlopen', side_effect=failing_http):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'rate_limit')
        self.assertEqual(report['message'], self.daily.MESSAGES['rate_limit'])
        self.assertEqual([failure['reason'] for failure in report['failures']], ['permission', 'rate_limit'])
        self.assertEqual(report['counts']['failedPosts'], 2)
        self.assertEqual(report['counts']['unprocessedObservations'], 1)
        self.assertEqual(len(self.requests), 2)

    def test_seed_photo_missing_from_parent_album_blocks_download(self):
        self.contents = {'103': b'negative', '104': b'no-face'}
        report = self.run_batch()
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'invalid_response')
        self.assertEqual(report['counts']['inspectedPhotos'], 0)
        self.assertTrue(all(urlsplit(request.full_url).hostname == 'graph.facebook.com' for request in self.requests))

    def test_overlapping_album_page_blocks_before_photo_download(self):
        post = self.post()
        children = post['attachments']['data'][0]['subattachments']
        children['summary'] = {'total_count': 3}
        children['paging'] = {'next': 'https://graph.facebook.com/v24.0/album/photos?after=NEXT'}
        with mock.patch.object(self, 'post', return_value=post):
            report = self.run_batch()
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'invalid_response')
        self.assertEqual(report['counts']['inspectedPhotos'], 0)
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(urlsplit(request.full_url).hostname == 'graph.facebook.com' for request in self.requests))

    def test_audit_preserves_historical_counts_on_cache_and_force_rechecks(self):
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            fresh = self.daily.run_daily(self.root, discovery_file=self.discovery)
            cached = self.daily.run_daily(self.root, discovery_file=self.discovery)
            forced = self.daily_run(self.root, discovery_file=self.discovery, force_recheck=True)
        audit = fresh['posts'][0]
        self.assertEqual(audit['photoCount'], 2)
        self.assertEqual(audit['inspectedPhotoCount'], 2)
        self.assertEqual(audit['candidatePhotoCount'], 0)
        self.assertEqual(audit['retainedPhotoCount'], 0)
        self.assertEqual(audit['checkMode'], 'fresh')
        self.assertEqual(audit['newInspectedPhotoCount'], 2)
        self.assertEqual(audit['sourceUrl'], SOURCE)
        self.assertEqual(audit['title'], 'Class photos')
        self.assertEqual(audit['publishedAt'], '2026-10-30T01:00:00+00:00')
        self.assertEqual(audit['checkedAt'], fresh['startedAt'])
        self.assertEqual(audit['attachments']['state'], 'complete')
        self.assertEqual(audit['attachments']['requestedLimit'], 100)
        self.assertEqual(cached['posts'][0]['checkMode'], 'cached')
        self.assertEqual(cached['posts'][0]['inspectedPhotoCount'], 2)
        self.assertEqual(cached['posts'][0]['checkedAt'], audit['checkedAt'])
        self.assertEqual(cached['posts'][0]['newInspectedPhotoCount'], 0)
        self.assertEqual(cached['counts']['cachedPosts'], 1)
        self.assertEqual(cached['counts']['inspectedPhotos'], 0)
        self.assertEqual(forced['posts'][0]['checkMode'], 'fresh')
        self.assertEqual(forced['counts']['inspectedPhotos'], 2)

    def test_cache_without_current_completeness_version_is_rechecked(self):
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            self.daily.run_daily(self.root, discovery_file=self.discovery)
            state_path = self.root / 'data/daily-state.json'
            state = json.loads(state_path.read_text())
            state['posts']['42_900'].pop('resolverVersion', None)
            state_path.write_text(json.dumps(state))
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['counts']['inspectedPhotos'], 2)
        self.assertEqual(report['counts']['cachedPosts'], 0)

    def test_reference_margin_config_change_invalidates_daily_check_cache(self):
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            self.daily.run_daily(self.root, discovery_file=self.discovery)
            cached = self.daily.run_daily(self.root, discovery_file=self.discovery)
            config = json.loads((self.root / 'config/job.json').read_text())
            config['referenceThresholdMargin'] = 0.015
            (self.root / 'config/job.json').write_text(json.dumps(config))
            changed = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(cached['counts']['cachedPosts'], 1)
        self.assertEqual(changed['counts']['cachedPosts'], 0)
        self.assertEqual(changed['counts']['inspectedPhotos'], 2)
        self.assertEqual(changed['posts'][0]['checkMode'], 'fresh')

    def test_replaced_gallery_invalidates_retained_count_cache(self):
        self.contents['100'] = b'candidate'
        (self.root / 'config/job.json').write_text(json.dumps({'version': 1, 'browserProfile': 'data/browser-profile', 'autoLike': False}))
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            first = self.daily.run_daily(self.root, discovery_file=self.discovery)
            paths = []
            for index, content in enumerate([b'candidate', b'no-face']):
                path = self.root / 'replacement-{}.jpg'.format(index)
                path.write_bytes(JPEG + content)
                paths.append(path)
            replacement = media.import_media(self.root, 'photos', SOURCE, paths, source_id='facebook:42_900')
            second = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertNotEqual(first['posts'][0]['galleryPostId'], replacement['id'])
        self.assertEqual(second['counts']['cachedPosts'], 0)
        self.assertEqual(second['counts']['inspectedPhotos'], 2)
        self.assertEqual(second['posts'][0]['retainedPhotoCount'], 2)
        self.assertEqual(second['posts'][0]['galleryPostId'], replacement['id'])
        self.assertEqual(common.load_manifest(self.root)['posts'][0]['recognition']['state'], 'complete')

    def test_changed_original_or_recognition_invalidates_gallery_cache(self):
        self.contents['100'] = b'candidate'
        (self.root / 'config/job.json').write_text(json.dumps({'version': 1, 'browserProfile': 'data/browser-profile', 'autoLike': False}))
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            self.daily.run_daily(self.root, discovery_file=self.discovery)
            post = common.load_manifest(self.root)['posts'][0]
            path = self.root / 'data' / post['assets'][0]['path']
            path.write_bytes(JPEG + b'negative')
            changed_file = self.daily.run_daily(self.root, discovery_file=self.discovery)
            manifest = common.load_manifest(self.root)
            manifest['posts'][0]['recognition']['state'] = 'pending'
            common.save_manifest(self.root, manifest)
            changed_recognition = self.daily.run_daily(self.root, discovery_file=self.discovery)
        for report in [changed_file, changed_recognition]:
            self.assertEqual(report['counts']['cachedPosts'], 0)
            self.assertEqual(report['counts']['inspectedPhotos'], 2)
            self.assertEqual(report['posts'][0]['checkMode'], 'fresh')

    def test_missing_original_never_reports_a_cached_complete_check(self):
        self.contents['100'] = b'candidate'
        (self.root / 'config/job.json').write_text(json.dumps({'version': 1, 'browserProfile': 'data/browser-profile', 'autoLike': False}))
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            self.daily.run_daily(self.root, discovery_file=self.discovery)
            post = common.load_manifest(self.root)['posts'][0]
            (self.root / 'data' / post['assets'][0]['path']).unlink()
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['counts']['cachedPosts'], 0)
        self.assertEqual(report['counts']['failedPosts'], 1)
        self.assertEqual(report['state'], 'blocked')

    def test_only_candidate_files_are_promoted_and_simulation_does_not_like(self):
        self.contents['100'] = b'candidate'
        with mock.patch.object(self.daily.subprocess, 'run', side_effect=AssertionError('simulation launched browser')):
            report = self.run_batch()
        posts = common.load_manifest(self.root)['posts']
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(posts[0]['assets']), 1)
        self.assertEqual(posts[0]['sourceIdentity'], 'facebook:42_900')
        self.assertEqual(posts[0]['recognition']['results'][0]['state'], 'candidate')
        self.assertIn(b'candidate', (self.root / 'data' / posts[0]['assets'][0]['path']).read_bytes())
        self.assertEqual(report['counts']['candidatePhotos'], 1)
        self.assertEqual(report['counts']['likedPhotos'], 0)
        self.assertTrue(report['simulated'])

    def test_existing_full_album_is_rechecked_without_replacing_originals(self):
        files = []
        for index, content in enumerate((b'negative', b'no-face'), 1):
            path = self.root / '{}.jpg'.format(index)
            path.write_bytes(JPEG + content)
            files.append(path)
        existing = media.import_media(self.root, 'photos', SOURCE, files, source_id='facebook:42_900')
        before = existing['assets']
        report = self.run_batch()
        current = common.load_manifest(self.root)['posts'][0]
        self.assertEqual([{key: value for key, value in asset.items() if key != 'sourcePhotoId'}
                          for asset in current['assets']], before)
        self.assertEqual([asset['sourcePhotoId'] for asset in current['assets']], ['100', '102'])
        self.assertEqual([value['state'] for value in current['recognition']['results']], ['no_match', 'no_face'])
        self.assertEqual(report['counts']['processedPosts'], 1)
        self.assertEqual(len(list((self.root / 'data/media').glob('**/*.jpg'))), 2)

    def test_partial_post_failure_preserves_success_and_safe_error(self):
        self.write_discovery([{'kind': 'photos', 'photoId': photo_id, 'sourceUrl': SOURCE} for photo_id in ('100', '200')])
        self.failed_posts.add('42_901')
        report = self.run_batch()
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['counts']['processedPosts'], 1)
        self.assertEqual(report['counts']['failedPosts'], 1)
        self.assertEqual(report['failures'][0]['reason'], 'permission')
        self.assertNotIn('DO-NOT-LOG', json.dumps(report))

    def test_wrong_page_and_mixed_video_album_never_download_or_import(self):
        for wrong_page, mixed in ((True, False), (False, True)):
            with self.subTest(wrong_page=wrong_page, mixed=mixed):
                self.wrong_page, self.mixed = wrong_page, mixed
                self.requests.clear()
                report = self.run_batch()
                self.assertEqual(report['counts']['inspectedPhotos'], 0)
                self.assertEqual(report['state'], 'blocked')
                self.assertTrue(all(urlsplit(request.full_url).hostname == 'graph.facebook.com' for request in self.requests))
                self.assertEqual(common.load_manifest(self.root)['posts'], [])

    def test_duplicate_parent_ids_and_changed_references_are_handled(self):
        self.write_discovery([{'kind': 'photos', 'photoId': photo_id, 'sourceUrl': SOURCE} for photo_id in ('100', '102')])
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
            first = self.daily.run_daily(self.root, discovery_file=self.discovery)
            self.assertEqual(first['counts']['processedPosts'], 1)
            self.assertEqual(first['counts']['duplicatePosts'], 1)
            self.requests.clear()
            second = self.daily.run_daily(self.root, discovery_file=self.discovery)
            self.assertEqual(second['counts']['skippedPosts'], 1)
            self.assertEqual(second['counts']['inspectedPhotos'], 0)
            (self.root / 'data/reference-child/private-child-name.jpg').write_bytes(JPEG + b'reference changed')
            third = self.daily.run_daily(self.root, discovery_file=self.discovery)
            self.assertEqual(third['counts']['inspectedPhotos'], 2)
        replay = self.run_batch()
        self.assertEqual(replay['counts']['inspectedPhotos'], 2)

    def test_missing_models_prevents_network_and_keeps_old_gallery(self):
        (self.root / 'data/models' / recognize.MODEL_NAMES[0]).unlink()
        with mock.patch.object(facebook, 'urlopen', side_effect=AssertionError('network before prerequisites')):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'recognition_prerequisites')
        self.assertEqual(report['counts']['inspectedPhotos'], 0)

    def test_batch_lock_returns_blocked_without_overwriting_running_status(self):
        (self.root / 'data/.daily.lock').touch()
        with (self.root / 'data/.daily.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = self.run_batch()
            self.assertEqual(report['state'], 'blocked')
            self.assertEqual(report['reason'], 'batch_locked')
            self.assertFalse((self.root / 'data/manifest.json').exists())
            archived = list((self.root / 'data/runs').glob('*.json'))
            self.assertEqual(len(archived), 1)
            self.assertEqual(json.loads(archived[0].read_text()), report)

    def test_source_secret_is_rejected_and_never_logged(self):
        self.write_discovery([{'kind': 'photos', 'photoId': '100', 'sourceUrl': SOURCE + '&access_token=DO-NOT-LOG-URL'}])
        report = self.run_batch()
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'invalid_discovery')
        self.assertNotIn('DO-NOT-LOG', json.dumps(report))
        self.assertEqual(self.requests, [])

    def test_live_collector_partial_status_is_retained_and_child_env_has_no_token(self):
        discovery = json.loads(self.discovery.read_text())
        discovery['state'] = 'partial'
        discovery['reason'] = 'scan_limit'
        def collector(argv, **options):
            self.assertEqual(argv[2], 'discover')
            self.assertEqual(argv[3:], ['--profile', str(self.root / 'data/browser-profile'), '--max-scrolls', '12'])
            self.assertNotIn('FB_ACCESS_TOKEN', options['env'])
            self.assertNotIn('DO-NOT-LOG', ' '.join(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(discovery), stderr='browser private output')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=collector):
            report = self.daily.run_daily(self.root)
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['discovery']['mode'], 'live')
        self.assertEqual(report['reason'], 'scan_limit')
        self.assertNotIn('browser private output', json.dumps(report))

    def test_live_collector_blocked_browser_reasons_are_preserved_without_private_output(self):
        for reason in ('browser_dependency_missing', 'browser_missing', 'profile_missing',
                       'profile_unavailable', 'profile_in_use', 'browser_launch_failed',
                       'facebook_navigation_failed', 'login_cancelled', 'invalid_arguments',
                       'browser_unavailable', 'login_required', 'page_unavailable', 'feed_unavailable'):
            with self.subTest(reason=reason):
                discovery = {'version': 1, 'capturedAt': '2026-11-01T07:00:00+00:00',
                             'state': 'blocked', 'posts': [], 'skippedVideos': 0, 'reason': reason,
                             'message': 'DO-NOT-LOG /Users/private/browser-profile'}
                result = subprocess.CompletedProcess([], 2, stdout=json.dumps(discovery),
                                                     stderr='DO-NOT-LOG /Users/private/browser-profile')
                with mock.patch.object(facebook, 'urlopen', side_effect=AssertionError('blocked collector used network')), \
                     mock.patch.object(self.daily.subprocess, 'run', return_value=result):
                    report = self.daily.run_daily(self.root)
                self.assertEqual(report['state'], 'blocked')
                self.assertEqual(report['reason'], reason)
                self.assertEqual(report['discovery']['reason'], reason)
                self.assertEqual(report['counts']['discoveredPosts'], 0)
                self.assertNotIn('DO-NOT-LOG', json.dumps(report))
                self.assertNotIn('/Users/private', json.dumps(report))

    def test_unknown_blocked_collector_reason_does_not_assume_login_expired(self):
        for reason in ('unknown_browser_error', None):
            with self.subTest(reason=reason):
                discovery = {'version': 1, 'capturedAt': '2026-11-01T07:00:00+00:00',
                             'state': 'blocked', 'posts': [], 'reason': reason}
                result = subprocess.CompletedProcess([], 2, stdout=json.dumps(discovery), stderr='')
                with mock.patch.object(self.daily.subprocess, 'run', return_value=result):
                    report = self.daily.run_daily(self.root)
                self.assertEqual(report['reason'], 'browser_unavailable')
                self.assertEqual(report['discovery']['reason'], 'browser_unavailable')

    def test_live_collector_partial_feed_stall_preserves_reason_and_processes_observed_photos(self):
        discovery = json.loads(self.discovery.read_text())
        discovery.update(state='partial', reason='feed_stalled')
        result = subprocess.CompletedProcess([], 2, stdout=json.dumps(discovery), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', return_value=result):
            report = self.daily.run_daily(self.root)
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['reason'], 'feed_stalled')
        self.assertEqual(report['discovery']['reason'], 'feed_stalled')
        self.assertEqual(report['counts']['processedPosts'], 1)
        self.assertEqual(report['counts']['inspectedPhotos'], 2)

    def test_discovery_preserves_browser_startup_errors(self):
        for reason in ('browser_dependency_missing', 'browser_missing', 'profile_missing',
                       'profile_unavailable', 'profile_in_use', 'browser_launch_failed',
                       'facebook_navigation_failed', 'login_cancelled', 'invalid_arguments',
                       'browser_unavailable', 'login_required'):
            with self.subTest(reason=reason):
                with mock.patch.object(self.daily, '_browser', side_effect=self.daily.DailyError(reason)):
                    report = self.daily.run_daily(self.root)
                self.assertEqual(report['state'], 'blocked')
                self.assertEqual(report['reason'], reason)

    def test_malformed_discovery_remains_invalid_even_with_known_browser_reason(self):
        for captured_at, reason in (('invalid date', 'profile_missing'),
                                    ('2026-11-01T07:00:00+00:00', ['profile_missing'])):
            with self.subTest(captured_at=captured_at, reason=reason):
                discovery = {'version': 1, 'capturedAt': captured_at, 'state': 'blocked',
                             'posts': [], 'reason': reason}
                self.discovery.write_text(json.dumps(discovery))
                report = self.run_batch()
                self.assertEqual(report['reason'], 'invalid_discovery')

    def test_candidate_live_reaction_and_negative_suppression(self):
        self.contents['100'] = b'candidate'
        def react(argv, **options):
            self.assertEqual(argv[2], 'react-photo')
            self.assertEqual(argv[-2:], ['--photo-id', '100'])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'version': 1, 'state': 'complete', 'reaction': 'liked', 'photoId': argv[-1]}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['counts']['likedPhotos'], 1)
        self.contents['100'] = b'negative'
        (self.root / 'data/reference-child/private-child-name.jpg').write_bytes(JPEG + b'reference changed')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=AssertionError('negative liked')):
            negative = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(negative['counts']['likedPhotos'], 0)

    def test_network_does_not_hold_manifest_lock(self):
        def unlocked_http(request, timeout=30):
            with (self.root / 'data/.manifest.lock').open('a+') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
            return self.http(request, timeout)
        with mock.patch.object(facebook, 'urlopen', side_effect=unlocked_http):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery, simulate=True)
        self.assertEqual(report['state'], 'complete')

    def test_each_candidate_photo_is_liked_and_recorded_by_stable_photo_id(self):
        self.contents.update({'100': b'candidate one', '103': b'candidate two', '104': b'negative'})
        targets = []
        def react(argv, **options):
            self.assertEqual(argv[2], 'react-photo')
            self.assertEqual(argv[-2], '--photo-id')
            targets.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
                {'state': 'complete', 'reaction': 'liked', 'photoId': argv[-1]}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['state'], 'complete')
        self.assertEqual(targets, ['100', '103'])
        self.assertEqual(report['counts']['likedPhotos'], 2)
        self.assertEqual(report['posts'][0]['reactionTarget'], 'photo')
        self.assertEqual(report['posts'][0]['photoReactions'], [
            {'photoId': '100', 'reaction': 'liked'}, {'photoId': '103', 'reaction': 'liked'}])
        state = json.loads((self.root / 'data/daily-state.json').read_text())
        self.assertEqual(set(state['photoReactions']), {'100', '103'})

    def test_photo_reaction_retries_only_failed_photo_and_keeps_successful_one(self):
        self.contents.update({'100': b'candidate one', '103': b'candidate two'})
        targets = []
        fail_second = {'value': True}
        def react(argv, **options):
            target = argv[-1]
            self.assertEqual(argv[2], 'react-photo')
            targets.append(target)
            body = {'state': 'blocked'} if target == '103' and fail_second['value'] else {
                'state': 'complete', 'reaction': 'liked', 'photoId': target}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            first = self.daily.run_daily(self.root, discovery_file=self.discovery)
            fail_second['value'] = False
            second = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(first['state'], 'partial')
        self.assertEqual(first['counts']['likedPhotos'], 1)
        self.assertEqual(first['failures'][0]['photoId'], '103')
        self.assertEqual(second['state'], 'complete')
        self.assertEqual(second['counts']['inspectedPhotos'], 0)
        self.assertEqual(second['counts']['likedPhotos'], 1)
        self.assertEqual(targets, ['100', '103', '103'])

    def test_new_photo_candidate_is_liked_without_repeating_previous_photo_after_recheck(self):
        self.contents['100'] = b'candidate'
        targets = []
        def react(argv, **options):
            self.assertEqual(argv[2], 'react-photo')
            targets.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
                {'state': 'complete', 'reaction': 'liked', 'photoId': argv[-1]}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            first = self.daily.run_daily(self.root, discovery_file=self.discovery)
            self.contents['103'] = b'candidate new'
            second = self.daily.run_daily(self.root, discovery_file=self.discovery, force_recheck=True)
        self.assertEqual(first['counts']['likedPhotos'], 1)
        self.assertEqual(second['counts']['likedPhotos'], 1)
        self.assertEqual(targets, ['100', '103'])

    def test_excluding_a_previously_liked_photo_never_unlikes_or_discards_reaction_record(self):
        self.contents['100'] = b'candidate'
        def react(argv, **options):
            self.assertEqual(argv[2], 'react-photo')
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
                {'state': 'complete', 'reaction': 'liked', 'photoId': argv[-1]}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            first = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(first['counts']['likedPhotos'], 1)
        gallery = common.load_manifest(self.root)['posts'][0]
        state_path = self.root / 'data/daily-state.json'
        liked = json.loads(state_path.read_text())['photoReactions'].copy()
        with mock.patch.object(self.daily.subprocess, 'run', side_effect=AssertionError('exclusion must not change FB reactions')):
            feedback.set_feedback(self.root, gallery['id'], gallery['assets'][0]['id'], True)
            with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
                excluded = self.daily.run_daily(self.root, discovery_file=self.discovery, force_recheck=True)
            feedback.set_feedback(self.root, gallery['id'], gallery['assets'][0]['id'], False)
            with mock.patch.object(facebook, 'urlopen', side_effect=self.http):
                restored = self.daily.run_daily(self.root, discovery_file=self.discovery, force_recheck=True)
        self.assertEqual(excluded['counts']['candidatePhotos'], 0)
        self.assertEqual(restored['counts']['candidatePhotos'], 1)
        self.assertEqual(excluded['counts']['likedPhotos'], 0)
        self.assertEqual(restored['counts']['likedPhotos'], 0)
        self.assertEqual(json.loads(state_path.read_text())['photoReactions'], liked)

    def test_post_level_like_history_does_not_suppress_photo_like(self):
        self.contents['100'] = b'candidate'
        self.run_batch()
        state_path = self.root / 'data/daily-state.json'
        state = json.loads(state_path.read_text())
        state['posts']['42_900']['audit']['reaction'] = 'liked'
        state['posts']['42_900']['audit'].pop('photoReactions', None)
        state.pop('photoReactions', None)
        state_path.write_text(json.dumps(state))
        targets = []
        def react(argv, **options):
            self.assertEqual(argv[2], 'react-photo')
            targets.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
                {'state': 'complete', 'reaction': 'liked', 'photoId': argv[-1]}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(targets, ['100'])
        self.assertEqual(report['counts']['cachedPosts'], 1)
        self.assertEqual(report['counts']['likedPhotos'], 1)

    def test_reaction_response_for_wrong_photo_does_not_record_success(self):
        self.contents['100'] = b'candidate'
        def react(argv, **options):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
                {'state': 'complete', 'reaction': 'liked', 'photoId': '999'}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['counts']['likedPhotos'], 0)
        self.assertEqual(report['posts'][0]['photoReactions'], [{'photoId': '100', 'reaction': 'failed'}])
        state = json.loads((self.root / 'data/daily-state.json').read_text())
        self.assertNotIn('100', state['photoReactions'])

    def test_exclusion_during_last_photo_like_keeps_like_and_stops_stale_result_claims(self):
        self.contents['100'] = b'candidate'
        targets = []
        def react(argv, **options):
            targets.append(argv[-1])
            gallery = common.load_manifest(self.root)['posts'][0]
            feedback.set_feedback(self.root, gallery['id'], gallery['assets'][0]['id'], True)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
                {'state': 'complete', 'reaction': 'liked', 'photoId': argv[-1]}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'feedback_changed')
        self.assertEqual(report['counts']['likedPhotos'], 1)
        self.assertEqual(report['counts']['processedPosts'], 0)
        self.assertEqual(report['posts'], [])
        self.assertEqual(targets, ['100'])
        ledger = json.loads((self.root / 'data/daily-state.json').read_text())['photoReactions']
        self.assertEqual(ledger['100']['reaction'], 'liked')
        self.assertTrue(common.load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])

    def test_cdn_url_cannot_be_rewritten_as_a_post_permalink(self):
        real_post = self.post
        def invalid_post(identifier='42_900'):
            post = real_post(identifier)
            post['permalink_url'] = 'https://scontent.fbcdn.net/private.jpg?signature=DO-NOT-LOG'
            return post
        self.post = invalid_post
        report = self.run_batch()
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['counts']['inspectedPhotos'], 0)

    def test_simulation_cache_still_allows_later_live_conditional_reaction(self):
        self.contents['100'] = b'candidate'
        self.run_batch()
        def react(argv, **options):
            self.assertEqual(argv[2], 'react-photo')
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'version': 1, 'state': 'complete', 'reaction': 'liked', 'photoId': argv[-1]}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=react):
            report = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(report['counts']['inspectedPhotos'], 0)
        self.assertEqual(report['counts']['likedPhotos'], 1)
        self.assertEqual(len(common.load_manifest(self.root)['posts']), 1)

    def test_new_candidates_append_to_identified_album_without_replacing_old_files(self):
        self.contents['100'] = b'candidate'
        first = self.run_batch()
        original = common.load_manifest(self.root)['posts'][0]
        self.contents['103'] = b'candidate third photo'
        report = self.run_batch()
        current = common.load_manifest(self.root)['posts'][0]
        self.assertEqual(len(current['assets']), 2)
        self.assertEqual(current['id'], original['id'])
        self.assertEqual(current['assets'][0], original['assets'][0])
        self.assertEqual(current['dailyPhotoIds'], ['100', '103'])
        self.assertEqual(report['counts']['candidatePhotos'], 2)
        self.assertEqual(first['counts']['candidatePhotos'], 1)

    def test_recognition_policy_metadata_and_job_aliases_are_preserved(self):
        result = {'state': 'no_match', 'score': 0.4, 'faceCount': 1, 'method': 'reference_consensus',
                  'referenceCount': 7, 'usableReferenceCount': 6, 'excludedReferenceCount': 1, 'effectiveThreshold': 0.6019,
                  'features': [[9, 8, 7]], 'referencePath': 'private-child-name.jpg'}
        with mock.patch.object(recognize, 'analyze_image', return_value=result):
            report = self.run_batch()
        evidence = report['posts'][0]['results'][0]
        self.assertEqual(evidence['effectiveThreshold'], 0.6019)
        self.assertEqual(evidence['usableReferenceCount'], 6)
        self.assertNotIn('features', evidence)
        self.assertNotIn('referencePath', evidence)
        self.assertEqual(report['discoveryMode'], 'replay')
        self.assertEqual(report['lastRunAt'], report['finishedAt'])

    def test_same_count_legacy_album_changes_do_not_drop_new_candidate(self):
        files = []
        for index in (1, 2):
            path = self.root / '{}.jpg'.format(index)
            path.write_bytes(JPEG + b'negative original ' + str(index).encode())
            files.append(path)
        existing = media.import_media(self.root, 'photos', SOURCE, files, source_id='facebook:42_900')
        self.contents = {'103': b'candidate new photo', '104': b'no-face'}
        self.write_discovery([{'kind': 'photos', 'photoId': '103', 'sourceUrl': SOURCE}])
        report = self.run_batch()
        current = common.load_manifest(self.root)['posts'][0]
        self.assertEqual(current['assets'][:2], existing['assets'])
        self.assertEqual(len(current['assets']), 3)
        self.assertEqual(current['recognition']['results'][2]['state'], 'candidate')
        self.assertEqual(report['counts']['candidatePhotos'], 1)

    def test_failed_reaction_is_retried_from_audit_without_redownloading(self):
        self.contents['100'] = b'candidate'
        def blocked_react(argv, **options):
            return subprocess.CompletedProcess(argv, 2, stdout=json.dumps({'state': 'blocked', 'reason': 'login_required'}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=blocked_react):
            first = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(first['state'], 'partial')
        self.assertEqual(first['posts'][0]['reaction'], 'failed')
        def successful_react(argv, **options):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'state': 'complete', 'reaction': 'liked', 'photoId': argv[-1]}), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=successful_react):
            retried = self.daily.run_daily(self.root, discovery_file=self.discovery)
        self.assertEqual(retried['state'], 'complete')
        self.assertEqual(retried['counts']['inspectedPhotos'], 0)
        self.assertEqual(retried['counts']['likedPhotos'], 1)

    def test_successful_fresh_reactions_are_not_requested_again_on_cache(self):
        self.contents['100'] = b'candidate'
        for reaction, expected_likes in [('liked', 1), ('already_liked', 0)]:
            with self.subTest(reaction=reaction):
                root = self.root / reaction
                (root / 'data/reference-child').mkdir(parents=True)
                (root / 'data/reference-child/reference.jpg').write_bytes(JPEG + b'reference')
                (root / 'data/models').mkdir()
                for name in recognize.MODEL_NAMES:
                    (root / 'data/models' / name).write_bytes(b'model' * 1024)
                (root / 'config').mkdir()
                (root / 'config/job.json').write_text(json.dumps({'version': 1, 'browserProfile': 'data/browser-profile', 'autoLike': True}))
                requests = []
                def browser_action(*args):
                    requests.append((args[1], args[3]))
                    return {'state': 'complete', 'reaction': reaction, 'photoId': args[3]}
                with mock.patch.object(facebook, 'urlopen', side_effect=self.http), \
                        mock.patch.object(self.daily, '_browser', side_effect=browser_action):
                    fresh = self.daily.run_daily(root, discovery_file=self.discovery)
                    cached = self.daily.run_daily(root, discovery_file=self.discovery)
                self.assertEqual(fresh['posts'][0]['reaction'], reaction)
                self.assertEqual(fresh['counts']['likedPhotos'], expected_likes)
                self.assertEqual(cached['posts'][0]['reaction'], reaction)
                self.assertEqual(cached['counts']['likedPhotos'], 0)
                self.assertEqual(cached['counts']['cachedPosts'], 1)
                self.assertEqual(requests, [('react-photo', '100')])

    def test_live_scan_limit_comes_from_job_configuration(self):
        (self.root / 'config/job.json').write_text(json.dumps({'version': 1, 'browserProfile': 'data/browser-profile', 'maxScrolls': 7}))
        discovery = json.loads(self.discovery.read_text())
        def collector(argv, **options):
            self.assertEqual(argv[-2:], ['--max-scrolls', '7'])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(discovery), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=collector):
            report = self.daily.run_daily(self.root)
        self.assertEqual(report['state'], 'complete')

    def test_manifest_save_failure_archives_the_returned_blocked_report(self):
        with mock.patch.object(self.daily, 'save_manifest', side_effect=OSError('private-child-name DO-NOT-LOG')):
            report = self.run_batch()
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'local_storage')
        archived = list((self.root / 'data/runs').glob('*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text()), report)
        self.assertNotIn('DO-NOT-LOG', json.dumps(report))

    def test_verified_public_page_alias_resolves_canonical_permission_error(self):
        parent = '864287026968664_200000000000001'
        alias = '100063963161862_200000000000001'
        requested = []
        def graph_http(request, timeout=30):
            identifier = urlsplit(request.full_url).path.rsplit('/', 1)[1]
            requested.append(identifier)
            if identifier == '200000000000002':
                return Response({'id': identifier, 'page_story_id': parent})
            if identifier == parent:
                return Response({'error': {'code': 200, 'message': 'DO-NOT-LOG'}})
            self.assertEqual(identifier, alias)
            post = self.post(parent)
            post['from'] = {'id': '864287026968664'}
            post['attachments']['data'][0]['subattachments']['data'][0]['target']['id'] = '200000000000002'
            return Response(post)
        with mock.patch.object(facebook, 'urlopen', side_effect=graph_http):
            result = self.daily.resolve_photo_post('200000000000002', 'DO-NOT-LOG-TOKEN', 'v26.0', '864287026968664')
        self.assertEqual(result['sourceId'], parent)
        self.assertEqual(result['retrievalPostId'], alias)
        self.assertEqual(result['photoIds'], ['200000000000002', '102'])
        self.assertEqual(requested, ['200000000000002', parent, alias])

    def test_public_alias_still_requires_canonical_returned_post_and_author(self):
        parent = '864287026968664_200000000000001'
        alias = '100063963161862_200000000000001'
        for returned_id, author, reason in ((alias, '864287026968664', 'invalid_response'),
                                            (parent, '100063963161862', 'wrong_page')):
            with self.subTest(returned_id=returned_id, author=author):
                requested = []
                def graph_http(request, timeout=30):
                    identifier = urlsplit(request.full_url).path.rsplit('/', 1)[1]
                    requested.append(identifier)
                    if identifier == '200000000000002':
                        return Response({'id': identifier, 'page_story_id': parent})
                    if identifier == parent:
                        return Response({'error': {'code': 200}})
                    self.assertEqual(identifier, alias)
                    post = self.post(returned_id)
                    post['from'] = {'id': author}
                    return Response(post)
                with mock.patch.object(facebook, 'urlopen', side_effect=graph_http):
                    with self.assertRaises(common.MediaError) as failure:
                        self.daily.resolve_photo_post('200000000000002', 'DO-NOT-LOG-TOKEN', 'v26.0', '864287026968664')
                self.assertEqual(failure.exception.reason, reason)
                self.assertEqual(requested, ['200000000000002', parent, alias])

    def test_public_alias_is_not_guessed_for_other_errors_or_pages(self):
        for configured_page, parent, error_code in (('864287026968664', '864287026968664_900', 190),
                                                     ('864287026968664', '864287026968664_900', 100),
                                                     ('42', '42_900', 200),
                                                     ('864287026968664', '99_900', 200)):
            with self.subTest(configured_page=configured_page, parent=parent, error_code=error_code):
                requested = []
                def graph_http(request, timeout=30):
                    identifier = urlsplit(request.full_url).path.rsplit('/', 1)[1]
                    requested.append(identifier)
                    if identifier == '100':
                        return Response({'id': '100', 'page_story_id': parent})
                    self.assertEqual(identifier, parent)
                    return Response({'error': {'code': error_code}})
                with mock.patch.object(facebook, 'urlopen', side_effect=graph_http):
                    with self.assertRaises(facebook.FetchError):
                        self.daily.resolve_photo_post('100', 'DO-NOT-LOG-TOKEN', 'v26.0', configured_page)
                self.assertEqual(requested, ['100', parent])

    def test_date_window_includes_whole_taipei_calendar_days(self):
        for published in ('2026-10-29T16:00:00+0000', '2026-11-01T15:59:59+0000'):
            with self.subTest(published=published):
                self.published_at = published
                report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                        start_date='2026-10-30', end_date='2026-11-01')
                self.assertEqual(report['range'], {'startDate': '2026-10-30', 'endDate': '2026-11-01', 'timeZone': 'Asia/Taipei'})
                self.assertEqual(report['state'], 'complete')
                self.assertEqual(report['counts']['processedPosts'], 1)
                self.assertEqual(report['counts']['inspectedPhotos'], 2)
                self.assertEqual(report['counts']['outOfRangePosts'], 0)
                self.assertEqual(report['counts']['missingDatePosts'], 0)

    def test_old_and_future_api_dates_skip_download_and_cached_reaction(self):
        self.contents['100'] = b'candidate'
        self.run_batch()
        for published in ('2026-10-30T01:00:00+0000', '2026-11-01T16:00:00+0000'):
            with self.subTest(published=published):
                self.published_at = published
                self.requests.clear()
                with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=AssertionError('out-of-range reaction')):
                    report = self.daily_run(self.root, discovery_file=self.discovery,
                                                  now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                                  start_date='2026-11-01', end_date='2026-11-01')
                self.assertEqual(report['state'], 'complete')
                self.assertEqual(report['counts']['outOfRangePosts'], 1)
                self.assertEqual(report['counts']['skippedPosts'], 1)
                self.assertEqual(report['counts']['inspectedPhotos'], 0)
                self.assertEqual(report['counts']['likedPhotos'], 0)
                self.assertEqual(report['posts'][0]['reason'], 'out_of_range')
                self.assertEqual(len(self.requests), 2)

    def test_excluded_and_missing_date_albums_skip_cap_validation(self):
        post = self.post()
        leaves = post['attachments']['data'][0]['subattachments']['data']
        post['attachments']['data'][0]['subattachments'] = {'data': leaves * 50}
        for published, state, reason in [('2026-10-20T01:00:00+0000', 'complete', 'out_of_range'),
                                         (None, 'partial', 'missing_date')]:
            with self.subTest(published=published):
                post['created_time'] = published
                self.requests.clear()
                with mock.patch.object(self, 'post', return_value=post):
                    report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                            start_date='2026-10-26', end_date='2026-11-01')
                self.assertEqual(report['state'], state)
                self.assertEqual(report['posts'][0]['reason'], reason)
                self.assertEqual(report['counts']['inspectedPhotos'], 0)
                self.assertEqual(report['counts']['failedPosts'], 0)
                self.assertEqual(len(self.requests), 2)

    def test_old_albums_do_not_follow_attachment_paging(self):
        for next_url in ['https://evil.test/v24.0/album/photos',
                         'https://graph.facebook.com/v24.0/album/photos?after=NEXT']:
            with self.subTest(next_url=next_url):
                post = self.post()
                post['created_time'] = '2026-10-20T01:00:00+0000'
                post['attachments']['data'][0]['subattachments']['paging'] = {'next': next_url}
                self.requests.clear()
                with mock.patch.object(self, 'post', return_value=post):
                    report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                            start_date='2026-10-26', end_date='2026-11-01')
                self.assertEqual(report['state'], 'complete')
                self.assertEqual(report['counts']['outOfRangePosts'], 1)
                self.assertEqual(len(self.requests), 2)

    def test_old_post_author_still_requires_configured_page(self):
        self.published_at = '2026-10-20T01:00:00+0000'
        self.wrong_page = True
        report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                start_date='2026-10-26', end_date='2026-11-01')
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'wrong_page')
        self.assertEqual(report['counts']['outOfRangePosts'], 0)

    def test_in_range_album_at_cap_still_fails_closed(self):
        post = self.post()
        leaves = post['attachments']['data'][0]['subattachments']['data']
        post['attachments']['data'][0]['subattachments'] = {'data': leaves * 50}
        with mock.patch.object(self, 'post', return_value=post):
            report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                    start_date='2026-10-26', end_date='2026-11-01')
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['reason'], 'pagination_limit')
        self.assertEqual(report['counts']['inspectedPhotos'], 0)

    def test_missing_invalid_or_naive_api_dates_are_partial_without_using_ui_dates(self):
        self.write_discovery([{'kind': 'photos', 'photoId': '100', 'sourceUrl': SOURCE,
                               'publishedAt': '2026-11-01T01:00:00+0800', 'date': '2026-11-01'}])
        for published in (None, '', '2026-02-30T01:00:00+0800', '2026-11-01T01:00:00',
                          '9999-12-31T23:59:59+0000'):
            with self.subTest(published=published):
                self.published_at = published
                self.requests.clear()
                report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                        start_date='2026-10-30', end_date='2026-11-01')
                self.assertEqual(report['state'], 'partial')
                self.assertEqual(report['reason'], 'missing_date')
                self.assertEqual(report['counts']['missingDatePosts'], 1)
                self.assertEqual(report['counts']['outOfRangePosts'], 0)
                self.assertEqual(report['counts']['failedPosts'], 0)
                self.assertEqual(report['counts']['skippedPosts'], 1)
                self.assertEqual(report['counts']['inspectedPhotos'], 0)
                self.assertEqual(report['posts'][0]['reason'], 'missing_date')
                self.assertEqual(len(self.requests), 2)

    def test_window_rejects_invalid_ranges_before_configuration_or_network(self):
        cases = ((None, '2026-11-01'), ('2026-10-30', None), ('', '2026-11-01'),
                 ('2026-1-01', '2026-11-01'), ('2026-02-30', '2026-11-01'),
                 ('2026-10-30', '2026-11-02'), ('2026-11-01', '2026-10-30'),
                 ('2026-07-31', '2026-11-01'), (20261030, '2026-11-01'))
        for start, end in cases:
            with self.subTest(start=start, end=end):
                with mock.patch.object(self.daily, '_configuration', side_effect=AssertionError('invalid range read configuration')):
                    report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc), start_date=start, end_date=end)
                self.assertEqual(report['state'], 'blocked')
                self.assertEqual(report['reason'], 'invalid_date_range')
                self.assertEqual(report['counts']['discoveredPosts'], 0)
                self.assertEqual(self.requests, [])

    def test_window_three_month_limit_clamps_month_end_and_uses_taipei_today(self):
        cases = ((datetime(2026, 5, 31, 4, tzinfo=timezone.utc), '2026-02-28', '2026-05-31'),
                 (datetime(2024, 5, 31, 4, tzinfo=timezone.utc), '2024-02-29', '2024-05-31'),
                 (datetime(2026, 10, 31, 16, 1, tzinfo=timezone.utc), '2026-08-01', '2026-11-01'))
        for now, start, end in cases:
            with self.subTest(start=start, end=end):
                self.published_at = end + 'T00:00:00+0800'
                report = self.run_batch(now=now, start_date=start, end_date=end)
                self.assertEqual(report['state'], 'complete')
                self.assertEqual(report['counts']['processedPosts'], 1)
                self.assertEqual(report['range']['endDate'], end)
        report = self.run_batch(now=datetime(2026, 5, 31, 4, tzinfo=timezone.utc),
                                start_date='2026-02-27', end_date='2026-05-31')
        self.assertEqual(report['reason'], 'invalid_date_range')

    def test_window_still_validates_post_author_before_excluding_its_date(self):
        self.wrong_page = True
        self.published_at = None
        report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                start_date='2026-10-30', end_date='2026-11-01')
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['failures'][0]['reason'], 'wrong_page')
        self.assertEqual(report['counts'].get('missingDatePosts'), 0)
        self.assertEqual(report['counts']['outOfRangePosts'], 0)

    def test_window_counts_duplicate_out_of_range_parent_only_once(self):
        self.write_discovery([{'kind': 'photos', 'photoId': value, 'sourceUrl': SOURCE} for value in ('100', '102')])
        report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                start_date='2026-11-01', end_date='2026-11-01')
        self.assertEqual(report['counts']['outOfRangePosts'], 1)
        self.assertEqual(report['counts']['duplicatePosts'], 1)
        self.assertEqual(report['counts']['skippedPosts'], 1)

    def test_no_window_retains_unbounded_replay_and_missing_date_failure(self):
        self.published_at = '2020-01-01T01:00:00+0000'
        report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc))
        self.assertEqual(report['state'], 'complete')
        self.assertEqual(report['counts']['processedPosts'], 1)
        self.assertNotIn('range', report)
        self.published_at = None
        report = self.run_batch(now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc))
        self.assertEqual(report['state'], 'blocked')
        self.assertEqual(report['failures'][0]['reason'], 'invalid_response')
        self.assertEqual(report['counts'].get('missingDatePosts'), 0)

    def test_manual_scroll_override_retains_honest_partial_scan_coverage(self):
        discovery = json.loads(self.discovery.read_text())
        discovery.update(state='partial', reason='scan_limit')
        def collector(argv, **options):
            self.assertEqual(argv[-2:], ['--max-scrolls', '100'])
            return subprocess.CompletedProcess(argv, 2, stdout=json.dumps(discovery), stderr='')
        with mock.patch.object(facebook, 'urlopen', side_effect=self.http), mock.patch.object(self.daily.subprocess, 'run', side_effect=collector):
            report = self.daily_run(self.root, now=datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                                          start_date='2026-10-30', end_date='2026-11-01', max_scrolls=100)
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['reason'], 'scan_limit')
        self.assertEqual(report['counts']['processedPosts'], 1)

    def test_scroll_override_rejects_noninteger_and_out_of_bounds_values(self):
        for value in (0, 101, True, '100', 0.5):
            with self.subTest(value=value):
                with mock.patch.object(self.daily, '_configuration', side_effect=AssertionError('invalid scroll read configuration')):
                    report = self.run_batch(max_scrolls=value)
                self.assertEqual(report['state'], 'blocked')
                self.assertEqual(report['reason'], 'configuration')
        self.assertEqual(self.requests, [])

    def test_cli_forwards_window_and_scroll_options(self):
        captured = {}
        def batch(*args, **options):
            captured.update(options)
            return {'state': 'complete', 'range': {'startDate': options.get('start_date'), 'endDate': options.get('end_date')}}
        output = io.StringIO()
        with mock.patch.object(self.daily, 'run_daily', side_effect=batch), redirect_stdout(output), redirect_stderr(io.StringIO()):
            try:
                status = self.daily.main(['--root', str(self.root), '--start-date', '2026-10-30',
                                          '--end-date', '2026-11-01', '--max-scrolls', '100', '--force-recheck'])
            except SystemExit:
                self.fail('daily CLI does not accept valid date/scan options')
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())['range'], {'startDate': '2026-10-30', 'endDate': '2026-11-01'})
        self.assertEqual(captured['max_scrolls'], 100)
        self.assertTrue(captured['force_recheck'])

    def test_discovery_coverage_preserves_only_safe_bounds(self):
        discovery = json.loads(self.discovery.read_text())
        discovery.update(state='partial', reason='scan_limit')
        discovery['coverage'] = {'state': 'partial', 'reason': 'scan_limit', 'scrolls': 7, 'snapshots': 8,
                                 'atBottom': False, 'observedPhotoPosts': 3, 'observedVideos': 2,
                                 'budget': 'scroll_limit', 'private': 'DO-NOT-LOG',
                                 'scroll': {'top': 2200, 'viewport': 800, 'height': 5200,
                                            'bottomDistance': 2200, 'container': 'document', 'url': 'DO-NOT-LOG'}}
        self.discovery.write_text(json.dumps(discovery))
        report = self.run_batch()
        coverage = report['discovery'].get('coverage', {})
        self.assertEqual(coverage.get('scrolls'), 7)
        self.assertEqual(coverage.get('observedPhotoPosts'), 3)
        self.assertEqual(coverage.get('budget'), 'scroll_limit')
        self.assertEqual(coverage.get('scroll', {}).get('bottomDistance'), 2200)
        self.assertNotIn('DO-NOT-LOG', json.dumps(report))

    def test_cli_rejects_invalid_scroll_limit_before_running_batch(self):
        for value in ('0', '101', '1.5', 'abc'):
            with self.subTest(value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    self.daily.main(['--root', str(self.root), '--max-scrolls', value])
                self.assertEqual(failure.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
