from pathlib import Path
from copy import deepcopy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from common import load_manifest, manifest_lock, save_manifest
import feedback


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.photo = self.root / 'data/media/local/photo.jpg'
        self.photo.parent.mkdir(parents=True)
        self.contents = b'\xff\xd8\xffsynthetic-photo'
        self.photo.write_bytes(self.contents)
        self.asset = {'id': 'asset-1', 'type': 'image', 'path': 'media/local/photo.jpg', 'name': 'photo.jpg'}
        manifest = load_manifest(self.root)
        manifest['posts'] = [{'id': 'post-1', 'kind': 'photos', 'assets': [self.asset], 'dailyPhotoIds': ['100'],
                              'recognition': {'state': 'complete', 'results': [{'assetId': 'asset-1', 'state': 'candidate'}]}}]
        save_manifest(self.root, manifest)

    def test_exclude_records_private_negative_and_keeps_original(self):
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.assertEqual(result, {'postId': 'post-1', 'assetId': 'asset-1', 'excluded': True,
                                  'negativeExampleCount': 1, 'revision': 1})
        record = feedback.load_feedback(self.root)['examples']['asset-1']
        self.assertEqual(record['photoId'], '100')
        self.assertEqual((self.root / 'data' / record['path']).read_bytes(), self.contents)
        self.assertEqual(self.photo.read_bytes(), self.contents)
        self.assertTrue(load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])
        self.assertEqual((self.root / 'data/feedback.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'data/negative-examples').stat().st_mode & 0o777, 0o700)
        self.assertNotIn('path', result)

    def test_undo_disables_learning_and_repeated_requests_are_idempotent(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        before = (self.root / 'data/feedback.json').read_bytes()
        self.assertEqual(feedback.set_feedback(self.root, 'post-1', 'asset-1', True)['revision'], 1)
        self.assertEqual((self.root / 'data/feedback.json').read_bytes(), before)
        undone = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(undone['negativeExampleCount'], 0)
        self.assertEqual(undone['revision'], 2)
        record = feedback.load_feedback(self.root)['examples']['asset-1']
        self.assertFalse(record['excluded'])
        self.assertTrue((self.root / 'data' / record['path']).is_file())
        self.assertFalse(load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])
        self.assertEqual(feedback.set_feedback(self.root, 'post-1', 'asset-1', False)['revision'], 2)

    def test_invalid_requests_do_not_change_files(self):
        original = (self.root / 'data/manifest.json').read_bytes()
        for post, asset, excluded in [('post-1', '../asset', True), ('unknown', 'asset-1', True),
                                     ('post-1', 'unknown', True), ('post-1', 'asset-1', 'true')]:
            with self.subTest(post=post, asset=asset, excluded=excluded):
                with self.assertRaises(feedback.FeedbackError):
                    feedback.set_feedback(self.root, post, asset, excluded)
        self.assertEqual((self.root / 'data/manifest.json').read_bytes(), original)
        self.assertFalse((self.root / 'data/feedback.json').exists())

    def test_missing_symlinked_or_invalid_original_fails_without_a_label(self):
        for variant in ['missing', 'symlink', 'bad-signature']:
            with self.subTest(variant=variant):
                if self.photo.exists() or self.photo.is_symlink(): self.photo.unlink()
                if variant == 'symlink':
                    other = self.root / 'outside.jpg'
                    other.write_bytes(self.contents)
                    self.photo.symlink_to(other)
                elif variant == 'bad-signature': self.photo.write_bytes(b'not an image')
                with self.assertRaises(feedback.FeedbackError):
                    feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
                self.assertFalse(load_manifest(self.root)['posts'][0]['assets'][0].get('userExcluded', False))

    def test_manifest_save_failure_rolls_feedback_back(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        before_feedback = (self.root / 'data/feedback.json').read_bytes()
        before_manifest = (self.root / 'data/manifest.json').read_bytes()
        with patch.object(feedback, 'save_manifest', side_effect=OSError('private-secret /private/path')):
            with self.assertRaises(feedback.FeedbackError) as failure:
                feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertNotIn('private-secret', str(failure.exception))
        self.assertEqual((self.root / 'data/feedback.json').read_bytes(), before_feedback)
        self.assertEqual((self.root / 'data/manifest.json').read_bytes(), before_manifest)

    def test_corrupt_feedback_and_symlinked_private_storage_are_rejected(self):
        path = self.root / 'data/feedback.json'
        path.write_text('{ private-secret')
        with self.assertRaises(feedback.FeedbackError) as failure:
            feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.assertNotIn('private-secret', str(failure.exception))
        path.unlink()
        directory = self.root / 'data/negative-examples'
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(feedback.FeedbackError):
            feedback.set_feedback(self.root, 'post-1', 'asset-1', True)

    def test_private_copy_cannot_be_tampered_and_reused(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        record = feedback.load_feedback(self.root)['examples']['asset-1']
        (self.root / 'data' / record['path']).write_bytes(b'\xff\xd8\xffchanged')
        with self.assertRaises(feedback.FeedbackError):
            feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.assertFalse(load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])

    def test_undo_preserves_the_original_negative_snapshot_after_gallery_bytes_change(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        original = feedback.load_feedback(self.root)['examples']['asset-1'].copy()
        self.photo.write_bytes(b'\xff\xd8\xffdifferent-original')
        feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        restored = feedback.load_feedback(self.root)['examples']['asset-1']
        self.assertFalse(restored['excluded'])
        self.assertEqual(restored['imageHash'], original['imageHash'])
        self.assertEqual(restored['path'], original['path'])

    def test_the_copied_byte_snapshot_itself_requires_an_image_signature(self):
        self.photo.write_bytes(b'not-an-image')
        read = feedback._regular_bytes
        def read_then_replace(path, limit):
            contents = read(path, limit)
            if path.resolve() == self.photo.resolve():
                self.photo.write_bytes(self.contents)
            return contents
        with patch.object(feedback, '_regular_bytes', side_effect=read_then_replace):
            with self.assertRaises(feedback.FeedbackError):
                feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.assertFalse(load_manifest(self.root)['posts'][0]['assets'][0].get('userExcluded', False))

    def test_known_label_can_be_undone_when_original_is_missing(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        original = feedback.load_feedback(self.root)['examples']['asset-1'].copy()
        self.photo.unlink()
        with self.assertRaises(feedback.FeedbackError):
            feedback.set_feedback(self.root, 'unknown', 'asset-1', False)
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(result['negativeExampleCount'], 0)
        self.assertFalse(load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])
        restored = feedback.load_feedback(self.root)['examples']['asset-1']
        self.assertEqual(restored['imageHash'], original['imageHash'])
        self.assertEqual(restored['path'], original['path'])
        self.assertFalse(restored['excluded'])

    def test_known_label_can_be_undone_with_missing_copy_and_invalid_original(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        original = feedback.load_feedback(self.root)['examples']['asset-1'].copy()
        (self.root / 'data' / original['path']).unlink()
        self.photo.write_bytes(b'unsupported-changed-original')
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(result['negativeExampleCount'], 0)
        self.assertFalse(load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])
        self.assertFalse(feedback.load_feedback(self.root)['examples']['asset-1']['excluded'])

    def add_photo(self, asset_id, post_id='post-1', photo_id=None, duplicate_of=None):
        path = self.photo.parent / (asset_id + '.jpg')
        path.write_bytes(b'\xff\xd8\xff' + asset_id.encode())
        asset = {'id': asset_id, 'type': 'image', 'path': 'media/local/' + path.name}
        if photo_id is not None:
            asset['sourcePhotoId'] = photo_id
        if duplicate_of is not None:
            asset['duplicateOf'] = duplicate_of
        manifest = load_manifest(self.root)
        post = next((item for item in manifest['posts'] if item['id'] == post_id), None)
        if post is None:
            post = {'id': post_id, 'kind': 'photos', 'assets': []}
            manifest['posts'].append(post)
        post.pop('dailyPhotoIds', None)
        post['assets'].append(asset)
        save_manifest(self.root, manifest)
        return path

    def flags(self):
        return {asset['id']: asset.get('userExcluded', False)
                for post in load_manifest(self.root)['posts'] for asset in post['assets']}

    def test_excluding_an_alias_marks_canonical_and_restore_clears_alias_learning(self):
        self.add_photo('asset-alias', duplicate_of='asset-1')
        result = feedback.set_feedback(self.root, 'post-1', 'asset-alias', True)
        self.assertEqual(result['assetId'], 'asset-alias')
        self.assertEqual(result['negativeExampleCount'], 1)
        self.assertEqual(self.flags(), {'asset-1': True, 'asset-alias': True})
        original = feedback.load_feedback(self.root)['examples']['asset-alias'].copy()
        restored = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(restored['negativeExampleCount'], 0)
        self.assertEqual(self.flags(), {'asset-1': False, 'asset-alias': False})
        record = feedback.load_feedback(self.root)['examples']['asset-alias']
        self.assertFalse(record['excluded'])
        self.assertEqual(record['imageHash'], original['imageHash'])
        self.assertEqual(record['path'], original['path'])

    def test_same_source_photo_id_across_posts_has_one_logical_negative_and_undo(self):
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        save_manifest(self.root, manifest)
        self.add_photo('asset-other-post', 'post-2', photo_id='100')
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        second = feedback.set_feedback(self.root, 'post-2', 'asset-other-post', True)
        self.assertEqual(second['negativeExampleCount'], 1)
        self.assertEqual(self.flags(), {'asset-1': True, 'asset-other-post': True})
        self.assertEqual(feedback.set_feedback(self.root, 'post-2', 'asset-other-post', False)['negativeExampleCount'], 0)
        self.assertTrue(all(not record['excluded'] for record in feedback.load_feedback(self.root)['examples'].values()))
        self.assertEqual(self.flags(), {'asset-1': False, 'asset-other-post': False})

    def test_distinct_identified_photos_are_independent_even_with_alias_metadata(self):
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        save_manifest(self.root, manifest)
        self.add_photo('asset-distinct', photo_id='200', duplicate_of='asset-1')
        feedback.set_feedback(self.root, 'post-1', 'asset-distinct', True)
        self.assertEqual(self.flags(), {'asset-1': False, 'asset-distinct': True})
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.assertEqual(feedback.set_feedback(self.root, 'post-1', 'asset-1', False)['negativeExampleCount'], 1)
        self.assertTrue(feedback.load_feedback(self.root)['examples']['asset-distinct']['excluded'])

    def test_undo_canonical_disables_preexisting_alias_records_without_original_bytes(self):
        alias = self.add_photo('asset-alias')
        feedback.set_feedback(self.root, 'post-1', 'asset-alias', True)
        original = feedback.load_feedback(self.root)['examples']['asset-alias'].copy()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][1]['duplicateOf'] = 'asset-1'
        save_manifest(self.root, manifest)
        alias.unlink()
        self.photo.unlink()
        (self.root / 'data' / original['path']).unlink()
        restored = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(restored['negativeExampleCount'], 0)
        self.assertEqual(self.flags(), {'asset-1': False, 'asset-alias': False})
        self.assertFalse(feedback.load_feedback(self.root)['examples']['asset-alias']['excluded'])

    def test_broken_and_cyclic_aliases_do_not_erase_unrelated_history(self):
        self.add_photo('asset-broken', duplicate_of='asset-missing')
        self.add_photo('asset-cycle-a', duplicate_of='asset-cycle-b')
        self.add_photo('asset-cycle-b', duplicate_of='asset-cycle-a')
        feedback.set_feedback(self.root, 'post-1', 'asset-broken', True)
        feedback.set_feedback(self.root, 'post-1', 'asset-cycle-a', True)
        feedback.set_feedback(self.root, 'post-1', 'asset-cycle-b', True)
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(result['negativeExampleCount'], 3)
        examples = feedback.load_feedback(self.root)['examples']
        self.assertTrue(examples['asset-broken']['excluded'])
        self.assertTrue(examples['asset-cycle-a']['excluded'])
        self.assertTrue(examples['asset-cycle-b']['excluded'])

    def test_group_save_failure_rolls_all_member_flags_and_history_back(self):
        self.add_photo('asset-alias', duplicate_of='asset-1')
        feedback.set_feedback(self.root, 'post-1', 'asset-alias', True)
        previous_feedback = (self.root / 'data/feedback.json').read_bytes()
        previous_manifest = (self.root / 'data/manifest.json').read_bytes()
        with patch.object(feedback, 'save_manifest', side_effect=OSError('failure')):
            with self.assertRaises(feedback.FeedbackError):
                feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual((self.root / 'data/feedback.json').read_bytes(), previous_feedback)
        self.assertEqual((self.root / 'data/manifest.json').read_bytes(), previous_manifest)

    def test_alias_chain_across_posts_restores_all_known_members(self):
        self.add_photo('asset-middle', 'post-2', duplicate_of='asset-1')
        self.add_photo('asset-alias', 'post-3', duplicate_of='asset-middle')
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.assertEqual(self.flags(), {'asset-1': True, 'asset-middle': True, 'asset-alias': True})
        result = feedback.set_feedback(self.root, 'post-3', 'asset-alias', False)
        self.assertEqual(result['negativeExampleCount'], 0)
        self.assertEqual(self.flags(), {'asset-1': False, 'asset-middle': False, 'asset-alias': False})

    def test_conflicting_identified_aliases_do_not_claim_an_unidentified_canonical(self):
        self.add_photo('asset-a', photo_id='100', duplicate_of='asset-1')
        self.add_photo('asset-b', photo_id='200', duplicate_of='asset-1')
        feedback.set_feedback(self.root, 'post-1', 'asset-a', True)
        self.assertEqual(self.flags(), {'asset-1': False, 'asset-a': True, 'asset-b': False})
        feedback.set_feedback(self.root, 'post-1', 'asset-b', True)
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(result['negativeExampleCount'], 2)
        self.assertTrue(feedback.load_feedback(self.root)['examples']['asset-a']['excluded'])
        self.assertTrue(feedback.load_feedback(self.root)['examples']['asset-b']['excluded'])

    def test_undo_source_identity_deactivates_retained_history_without_gallery_alias(self):
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        save_manifest(self.root, manifest)
        self.add_photo('asset-alias', 'post-2', photo_id='100')
        feedback.set_feedback(self.root, 'post-2', 'asset-alias', True)
        manifest = load_manifest(self.root)
        manifest['posts'].pop()
        save_manifest(self.root, manifest)
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(result['negativeExampleCount'], 0)
        self.assertFalse(feedback.load_feedback(self.root)['examples']['asset-alias']['excluded'])

    def test_conflicting_retained_source_identities_are_not_erased_by_new_alias_metadata(self):
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        save_manifest(self.root, manifest)
        self.add_photo('asset-distinct', photo_id='200')
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        feedback.set_feedback(self.root, 'post-1', 'asset-distinct', True)
        manifest = load_manifest(self.root)
        for asset in manifest['posts'][0]['assets']:
            asset.pop('sourcePhotoId', None)
        manifest['posts'][0]['assets'][1]['duplicateOf'] = 'asset-1'
        save_manifest(self.root, manifest)
        previous_feedback = (self.root / 'data/feedback.json').read_bytes()
        previous_manifest = (self.root / 'data/manifest.json').read_bytes()
        with self.assertRaises(feedback.FeedbackError):
            feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual((self.root / 'data/feedback.json').read_bytes(), previous_feedback)
        self.assertEqual((self.root / 'data/manifest.json').read_bytes(), previous_manifest)

    def test_reexclusion_preserves_a_retained_source_id_when_manifest_identity_disappears(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.add_photo('asset-alias', duplicate_of='asset-1')
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.assertEqual(feedback.load_feedback(self.root)['examples']['asset-1'].get('photoId'), '100')
        self.assertEqual(result['negativeExampleCount'], 1)
        result = feedback.set_feedback(self.root, 'post-1', 'asset-alias', True)
        self.assertEqual(result['negativeExampleCount'], 1)

    def test_inherited_source_identity_undo_disables_missing_members_history(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.add_photo('asset-other', 'post-2', photo_id='100')
        feedback.set_feedback(self.root, 'post-2', 'asset-other', True)
        manifest = load_manifest(self.root)
        manifest['posts'].pop()
        manifest['posts'][0].pop('dailyPhotoIds', None)
        save_manifest(self.root, manifest)
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(result['negativeExampleCount'], 0)
        self.assertTrue(all(not item['excluded'] for item in feedback.load_feedback(self.root)['examples'].values()))

    def test_numeric_history_expansion_restores_equivalent_member_flags_together(self):
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.add_photo('asset-other', 'post-2', photo_id='100')
        feedback.set_feedback(self.root, 'post-2', 'asset-other', True)
        manifest = load_manifest(self.root)
        manifest['posts'][1]['assets'][0].pop('sourcePhotoId')
        save_manifest(self.root, manifest)
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(result['negativeExampleCount'], 0)
        self.assertEqual(self.flags(), {'asset-1': False, 'asset-other': False})

    def test_inherited_identity_restores_current_members_without_feedback_records(self):
        self.add_photo('asset-other', 'post-2', photo_id='100')
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        save_manifest(self.root, manifest)
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        self.assertEqual(set(feedback.load_feedback(self.root)['examples']), {'asset-1'})
        self.assertEqual(self.flags(), {'asset-1': True, 'asset-other': True})
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0].pop('sourcePhotoId')
        manifest['posts'][0].pop('dailyPhotoIds', None)
        save_manifest(self.root, manifest)
        result = feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        self.assertEqual(result['negativeExampleCount'], 0)
        self.assertEqual(self.flags(), {'asset-1': False, 'asset-other': False})

    def test_canonical_ownership_uses_the_complete_conflicting_alias_family(self):
        first = {'id': 'asset-a', 'type': 'image', 'sourcePhotoId': '100', 'duplicateOf': 'asset-c'}
        canonical = {'id': 'asset-c', 'type': 'image', 'sourcePhotoId': '100'}
        conflicting = {'id': 'asset-b', 'type': 'image', 'sourcePhotoId': '200', 'duplicateOf': 'asset-c'}
        manifest = {'posts': [{'id': 'post-0', 'kind': 'photos', 'assets': [first]},
                              {'id': 'post-1', 'kind': 'photos', 'assets': [canonical, conflicting]}]}
        before = deepcopy(manifest)
        owners = feedback.identity_canonicals(manifest)
        self.assertEqual({asset_id: (post['id'], asset['id']) for asset_id, (post, asset) in owners.items()},
                         {'asset-a': ('post-0', 'asset-a'), 'asset-c': ('post-0', 'asset-a'),
                          'asset-b': ('post-1', 'asset-b')})
        self.assertEqual(manifest, before)

    def test_valid_aliases_select_the_original_owner_even_when_alias_appears_first(self):
        first = {'id': 'asset-a', 'type': 'image', 'sourcePhotoId': '100', 'duplicateOf': 'asset-c'}
        canonical = {'id': 'asset-c', 'type': 'image', 'sourcePhotoId': '100'}
        manifest = {'posts': [{'id': 'post-0', 'kind': 'photos', 'assets': [first]},
                              {'id': 'post-1', 'kind': 'photos', 'assets': [canonical]}]}
        owners = feedback.identity_canonicals(manifest)
        self.assertEqual({asset_id: (post['id'], asset['id']) for asset_id, (post, asset) in owners.items()},
                         {'asset-a': ('post-1', 'asset-c'), 'asset-c': ('post-1', 'asset-c')})

    def test_cyclic_aliases_do_not_override_first_numeric_identity_owner(self):
        first = {'id': 'asset-a', 'type': 'image', 'sourcePhotoId': '100', 'duplicateOf': 'asset-b'}
        second = {'id': 'asset-b', 'type': 'image', 'sourcePhotoId': '100', 'duplicateOf': 'asset-a'}
        manifest = {'posts': [{'id': 'post-0', 'kind': 'photos', 'assets': [first]},
                              {'id': 'post-1', 'kind': 'photos', 'assets': [second]}]}
        owners = feedback.identity_canonicals(manifest)
        self.assertEqual({asset_id: (post['id'], asset['id']) for asset_id, (post, asset) in owners.items()},
                         {'asset-a': ('post-0', 'asset-a'), 'asset-b': ('post-0', 'asset-a')})

    def legacy_label(self):
        manifest = load_manifest(self.root)
        manifest['posts'][0].pop('dailyPhotoIds', None)
        save_manifest(self.root, manifest)
        feedback.set_feedback(self.root, 'post-1', 'asset-1', True)
        return feedback.load_feedback(self.root)

    def commit_identity(self, manifest, backfills=None, expected=None, callback=None):
        with manifest_lock(self.root):
            return feedback.commit_identity_backfills(self.root, manifest, backfills,
                                                     expected_feedback=expected, before_commit=callback)

    def test_identity_commit_promotes_exact_legacy_label_without_changing_label_or_files(self):
        previous = self.legacy_label()
        private = self.root / 'data' / previous['examples']['asset-1']['path']
        private_contents = private.read_bytes()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        observed = []
        body = self.commit_identity(manifest, {'asset-1': '100'}, previous,
                                    lambda old, new: observed.append((deepcopy(old), deepcopy(new))))
        record = body['examples']['asset-1'].copy()
        self.assertEqual(record.pop('photoId'), '100')
        self.assertEqual(record, previous['examples']['asset-1'])
        self.assertEqual(body['revision'], previous['revision'] + 1)
        self.assertEqual(observed[0], (previous, body))
        self.assertEqual(private.read_bytes(), private_contents)
        self.assertEqual(self.photo.read_bytes(), self.contents)
        self.assertTrue(load_manifest(self.root)['posts'][0]['assets'][0]['userExcluded'])
        self.assertEqual(self.commit_identity(load_manifest(self.root), expected=body), body)

    def test_identity_warmup_promotes_aligned_array_and_valid_alias_identity(self):
        self.legacy_label()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['dailyPhotoIds'] = ['100']
        body = self.commit_identity(manifest)
        self.assertEqual(body['examples']['asset-1']['photoId'], '100')

    def test_identity_commit_rolls_both_snapshots_back_when_manifest_save_fails(self):
        previous = self.legacy_label()
        before_feedback = (self.root / 'data/feedback.json').read_bytes()
        before_manifest = (self.root / 'data/manifest.json').read_bytes()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        with patch.object(feedback, 'save_manifest', side_effect=OSError('private failure')):
            with self.assertRaises(feedback.FeedbackError):
                self.commit_identity(manifest, {'asset-1': '100'}, previous)
        self.assertEqual((self.root / 'data/feedback.json').read_bytes(), before_feedback)
        self.assertEqual((self.root / 'data/manifest.json').read_bytes(), before_manifest)

    def test_identity_commit_rejects_changed_parent_snapshot_before_any_write(self):
        previous = self.legacy_label()
        feedback.set_feedback(self.root, 'post-1', 'asset-1', False)
        before_feedback = (self.root / 'data/feedback.json').read_bytes()
        before_manifest = (self.root / 'data/manifest.json').read_bytes()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        with self.assertRaises(feedback.FeedbackError):
            self.commit_identity(manifest, {'asset-1': '100'}, previous)
        self.assertEqual((self.root / 'data/feedback.json').read_bytes(), before_feedback)
        self.assertEqual((self.root / 'data/manifest.json').read_bytes(), before_manifest)

    def test_identity_commit_rejects_conflicting_history_and_changed_original(self):
        for variant in ['conflicting-id', 'changed-original']:
            with self.subTest(variant=variant):
                previous = self.legacy_label()
                if variant == 'conflicting-id':
                    previous['examples']['asset-1']['photoId'] = '200'
                    feedback._atomic_bytes(self.root, 'feedback.json',
                                           (json.dumps(previous) + '\n').encode())
                else:
                    self.photo.write_bytes(b'\xff\xd8\xffchanged-original')
                before_feedback = (self.root / 'data/feedback.json').read_bytes()
                before_manifest = (self.root / 'data/manifest.json').read_bytes()
                manifest = load_manifest(self.root)
                manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
                with self.assertRaises(feedback.FeedbackError):
                    self.commit_identity(manifest, {'asset-1': '100'}, previous)
                self.assertEqual((self.root / 'data/feedback.json').read_bytes(), before_feedback)
                self.assertEqual((self.root / 'data/manifest.json').read_bytes(), before_manifest)
                self.photo.write_bytes(self.contents)
                (self.root / 'data/feedback.json').unlink()

    def test_identity_commit_callback_failure_does_not_publish_either_snapshot(self):
        previous = self.legacy_label()
        before_feedback = (self.root / 'data/feedback.json').read_bytes()
        before_manifest = (self.root / 'data/manifest.json').read_bytes()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        def reject(old, new):
            raise RuntimeError('external feedback changed')
        with self.assertRaises(RuntimeError):
            self.commit_identity(manifest, {'asset-1': '100'}, previous, reject)
        self.assertEqual((self.root / 'data/feedback.json').read_bytes(), before_feedback)
        self.assertEqual((self.root / 'data/manifest.json').read_bytes(), before_manifest)

    def test_identity_warmup_inherits_a_proven_valid_alias_source_id(self):
        self.legacy_label()
        self.add_photo('asset-alias', photo_id='100', duplicate_of='asset-1')
        manifest = load_manifest(self.root)
        body = self.commit_identity(manifest)
        self.assertEqual(body['examples']['asset-1']['photoId'], '100')

    def test_identity_commit_rejects_external_manifest_changes_during_callback(self):
        previous = self.legacy_label()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        external = load_manifest(self.root)
        external['posts'][0]['title'] = 'parent-change'
        def change_manifest(old, new):
            save_manifest(self.root, external)
        with self.assertRaises(feedback.FeedbackError):
            self.commit_identity(manifest, {'asset-1': '100'}, previous, change_manifest)
        self.assertEqual(load_manifest(self.root), external)
        self.assertEqual(feedback.load_feedback(self.root), previous)

    def test_identity_commit_does_not_adopt_parent_feedback_changes_during_source_validation(self):
        previous = self.legacy_label()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        external = deepcopy(previous)
        external['revision'] += 1
        external['examples']['asset-1']['excluded'] = False
        read = feedback._regular_bytes
        def change_after_read(path, limit):
            contents = read(path, limit)
            if path.resolve() == self.photo.resolve():
                feedback._atomic_bytes(self.root, 'feedback.json', (json.dumps(external) + '\n').encode())
            return contents
        with patch.object(feedback, '_regular_bytes', side_effect=change_after_read):
            with self.assertRaises(feedback.FeedbackError):
                self.commit_identity(manifest, {'asset-1': '100'}, previous)
        self.assertEqual(feedback.load_feedback(self.root), external)
        self.assertNotIn('sourcePhotoId', load_manifest(self.root)['posts'][0]['assets'][0])

    def test_identity_commit_does_not_adopt_external_feedback_after_the_first_read(self):
        previous = self.legacy_label()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        external = deepcopy(previous)
        external['revision'] += 1
        external['examples']['asset-1']['excluded'] = False
        read, changed = feedback._regular_bytes, []
        def change_after_first_read(path, limit):
            contents = read(path, limit)
            if path.name == 'feedback.json' and not changed:
                changed.append(True)
                feedback._atomic_bytes(self.root, 'feedback.json', (json.dumps(external) + '\n').encode())
            return contents
        with patch.object(feedback, '_regular_bytes', side_effect=change_after_first_read):
            with self.assertRaises(feedback.FeedbackError) as failure:
                self.commit_identity(manifest, {'asset-1': '100'})
        self.assertEqual(failure.exception.reason, 'feedback_changed')
        self.assertEqual(feedback.load_feedback(self.root), external)
        self.assertNotIn('sourcePhotoId', load_manifest(self.root)['posts'][0]['assets'][0])

    def test_identity_commit_does_not_adopt_external_manifest_after_the_first_parse(self):
        previous = self.legacy_label()
        manifest = load_manifest(self.root)
        manifest['posts'][0]['assets'][0]['sourcePhotoId'] = '100'
        external = load_manifest(self.root)
        external['posts'][0]['title'] = 'parent-change'
        read, changed = feedback.load_manifest, []
        def change_after_first_parse(root):
            parsed = read(root)
            if not changed:
                changed.append(True)
                save_manifest(self.root, external)
            return parsed
        with patch.object(feedback, 'load_manifest', side_effect=change_after_first_parse):
            with self.assertRaises(feedback.FeedbackError) as failure:
                self.commit_identity(manifest, {'asset-1': '100'})
        self.assertEqual(failure.exception.reason, 'feedback_changed')
        self.assertEqual(load_manifest(self.root), external)
        self.assertEqual(feedback.load_feedback(self.root), previous)


if __name__ == '__main__':
    unittest.main()
