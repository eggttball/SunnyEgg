import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import daily
import facebook
import manual_photo
import recognize


class Response:
    def __init__(self, value, content_type='application/json'):
        self.payload = json.dumps(value).encode() if isinstance(value, dict) else value
        self.headers = {'Content-Type': content_type}

    def read(self, size=-1):
        value = self.payload if size < 0 else self.payload[:size]
        self.payload = b'' if size < 0 else self.payload[size:]
        return value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class ManualPhotoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'config').mkdir()
        self.configure(True)
        self.env_file = self.root / '.env'
        self.env_file.write_text('FB_ACCESS_TOKEN=TEST_TOKEN\nFB_GRAPH_VERSION=v24.0\nFB_PAGE_ID=42\n')
        self.downloads = 0
        self.author = '42'
        for patch in [
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(facebook, 'urlopen', side_effect=self.http),
            mock.patch.object(recognize, 'FaceEngine', side_effect=AssertionError('Manual selection does not need recognition')),
        ]:
            patch.start()
            self.addCleanup(patch.stop)
        browser_patch = mock.patch.object(daily, '_browser', return_value={
            'state': 'complete', 'reaction': 'liked', 'photoId': '100',
        })
        self.browser = browser_patch.start()
        self.addCleanup(browser_patch.stop)

    def configure(self, enabled):
        (self.root / 'config/job.json').write_text(json.dumps({
            'version': 1, 'browserProfile': 'data/browser-profile', 'autoLike': enabled,
        }))

    def http(self, request, timeout=30):
        parts = urlsplit(request.full_url)
        if parts.hostname == 'scontent.fbcdn.net':
            self.downloads += 1
            return Response(b'\xff\xd8\xff\xe0fixture-photo', 'image/jpeg')
        self.assertEqual(parts.hostname, 'graph.facebook.com')
        fields = parse_qs(parts.query)['fields'][0]
        if fields == 'id,page_story_id':
            return Response({'id': '100', 'page_story_id': '42_900'})
        if fields == 'id,images':
            return Response({'id': '100', 'images': [{
                'width': 200, 'height': 100, 'source': 'https://scontent.fbcdn.net/photo.jpg',
            }]})
        return Response({
            'id': '42_900', 'from': {'id': self.author}, 'message': 'Fixture album',
            'created_time': '2026-11-05T10:00:00+0000',
            'permalink_url': 'https://www.facebook.com/permalink.php?story_fbid=900&id=42',
        })

    def run_import(self):
        return manual_photo.import_photo(self.root, self.env_file, '100')

    def asset(self):
        return common.load_manifest(self.root)['posts'][0]['assets'][0]

    def test_new_photo_is_liked_after_it_is_saved_without_recognition(self):
        result = self.run_import()
        self.assertEqual(self.browser.call_count, 1)
        self.assertEqual(self.browser.call_args.args[1], 'react-photo')
        self.assertEqual(self.browser.call_args.args[3], '100')
        self.assertEqual(result, {'state': 'complete', 'action': 'added', 'reaction': 'liked'})
        asset = self.asset()
        self.assertTrue(asset['manualSelection'])
        self.assertTrue((self.root / 'data' / asset['path']).is_file())
        state = json.loads((self.root / 'data/daily-state.json').read_text())
        self.assertEqual(state['photoReactions']['100']['reaction'], 'liked')

    def test_disabled_auto_like_only_saves_the_photo(self):
        self.configure(False)
        result = self.run_import()
        self.assertEqual(result['reaction'], 'not_requested')
        self.browser.assert_not_called()
        self.assertTrue(self.asset()['manualSelection'])

    def test_repeated_selection_uses_the_shared_reaction_record(self):
        self.run_import()
        result = self.run_import()
        self.assertEqual(result['action'], 'already_selected')
        self.assertEqual(result['reaction'], 'already_liked')
        self.assertEqual(self.browser.call_count, 1)
        self.assertEqual(self.downloads, 1)
        self.assertEqual(len(common.load_manifest(self.root)['posts'][0]['assets']), 1)

    def test_like_failure_preserves_the_photo_and_retries_without_downloading_again(self):
        self.browser.return_value = {'state': 'blocked', 'reason': 'login_required'}
        result = self.run_import()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['reaction'], 'failed')
        self.assertTrue((self.root / 'data' / self.asset()['path']).is_file())
        state_path = self.root / 'data/daily-state.json'
        self.assertFalse(state_path.exists())
        self.browser.return_value = {'state': 'complete', 'reaction': 'already_liked', 'photoId': '100'}
        result = self.run_import()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['action'], 'already_selected')
        self.assertEqual(result['reaction'], 'already_liked')
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.browser.call_count, 2)

    def test_excluded_photo_is_not_liked_or_restored(self):
        self.configure(False)
        self.run_import()
        with common.manifest_lock(self.root):
            manifest = common.load_manifest(self.root)
            manifest['posts'][0]['assets'][0]['userExcluded'] = True
            common.save_manifest(self.root, manifest)
        self.configure(True)
        result = self.run_import()
        self.assertEqual(result['action'], 'excluded')
        self.assertEqual(result['reaction'], 'not_requested')
        self.assertTrue(self.asset()['userExcluded'])
        self.browser.assert_not_called()

    def test_existing_gallery_photo_is_promoted_and_liked(self):
        self.configure(False)
        self.run_import()
        with common.manifest_lock(self.root):
            manifest = common.load_manifest(self.root)
            manifest['posts'][0]['assets'][0].pop('manualSelection')
            common.save_manifest(self.root, manifest)
        self.configure(True)
        result = self.run_import()
        self.assertEqual(result['action'], 'promoted')
        self.assertEqual(result['reaction'], 'liked')
        self.assertEqual(self.downloads, 1)

    def test_manual_like_preserves_existing_batch_state(self):
        (self.root / 'data').mkdir()
        state_path = self.root / 'data/daily-state.json'
        state_path.write_text(json.dumps({
            'version': 1, 'posts': {'42_800': {'audit': {'state': 'complete'}}},
            'photoReactions': {'200': {'reaction': 'liked', 'checkedAt': '2026-11-04T00:00:00+00:00'}},
        }))
        self.run_import()
        state = json.loads(state_path.read_text())
        self.assertEqual(state['posts'], {'42_800': {'audit': {'state': 'complete'}}})
        self.assertEqual(set(state['photoReactions']), {'100', '200'})

    def test_wrong_page_cannot_save_or_like_the_photo(self):
        self.author = '99'
        with self.assertRaises(daily.DailyError) as caught:
            self.run_import()
        self.assertEqual(caught.exception.reason, 'wrong_page')
        self.browser.assert_not_called()
        self.assertEqual(common.load_manifest(self.root)['posts'], [])

    def test_corrupt_like_record_reports_storage_failure_after_saving(self):
        (self.root / 'data').mkdir()
        (self.root / 'data/daily-state.json').write_text('invalid json')
        result = self.run_import()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['reason'], 'local_storage')
        self.assertTrue((self.root / 'data' / self.asset()['path']).is_file())
        self.browser.assert_not_called()

    def test_failed_save_cannot_like_the_photo(self):
        with mock.patch.object(manual_photo, 'save_manifest', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_import()
        self.browser.assert_not_called()
        self.assertEqual(common.load_manifest(self.root)['posts'], [])


if __name__ == '__main__':
    unittest.main()
