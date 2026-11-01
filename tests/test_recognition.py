import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from common import MediaError, load_manifest, save_manifest
from recognize import (reference_files, check_inputs, cosine_similarity, match_features,
                       analyze_image, analyze_video, recognize)


class RecognitionTests(unittest.TestCase):
    def embed_post(self):
        return {'id': 'embedded-video', 'kind': 'video', 'playback': 'facebook_embed',
                'sourceUrl': 'https://www.facebook.com/reel/123/', 'sourceIdentity': 'facebook-embed:123',
                'title': '影片來源', 'publishedAt': None, 'assets': [], 'clips': [],
                'recognition': {'state': 'not_required', 'message': '影片由 Facebook 播放器提供，未執行內容辨識。', 'results': []}}

    def test_specified_embed_needs_no_recognition_before_reference_or_model_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = load_manifest(root)
            manifest['posts'] = [self.embed_post()]
            save_manifest(root, manifest)
            before = (root / 'data/manifest.json').read_bytes()
            with patch('recognize.check_inputs', side_effect=AssertionError('embed must not read references/models')):
                with self.assertRaisesRegex(MediaError, '不需要.*辨識'):
                    recognize(root, post_id='embedded-video')
            self.assertEqual((root / 'data/manifest.json').read_bytes(), before)

    def test_embed_only_gallery_does_not_record_empty_success_or_load_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = load_manifest(root)
            manifest['posts'] = [self.embed_post()]
            save_manifest(root, manifest)
            with patch('recognize.check_inputs', side_effect=AssertionError('no local assets must not load models')):
                with self.assertRaisesRegex(MediaError, '本機.*素材'):
                    recognize(root)
            self.assertEqual(load_manifest(root)['posts'][0]['recognition']['state'], 'not_required')

    def test_mixed_gallery_recognizes_only_local_assets_and_keeps_embed_state(self):
        class Engine:
            def __init__(self, models): pass
            def describe_file(self, path): return [[1, 0]]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            embedded = self.embed_post()
            local = {'id': 'local-photo', 'kind': 'photos', 'assets': [{'id': 'photo-1', 'type': 'image', 'path': 'media/local/photo.jpg'}],
                     'recognition': {'state': 'pending', 'results': []}, 'clips': []}
            manifest = load_manifest(root)
            manifest['posts'] = [embedded, local]
            save_manifest(root, manifest)
            with patch('recognize.check_inputs', return_value=([root / 'reference.jpg'], [])), patch('recognize.FaceEngine', Engine):
                self.assertEqual(recognize(root), 1)
            posts = load_manifest(root)['posts']
            self.assertEqual(posts[0], embedded)
            self.assertEqual(posts[1]['recognition']['state'], 'complete')
            self.assertEqual(posts[1]['recognition']['results'][0]['assetId'], 'photo-1')

    def test_zero_padding_is_rejected_before_recognition_loads_inputs(self):
        with patch('recognize.load_manifest') as load, patch('recognize.check_inputs') as inputs:
            with self.assertRaisesRegex(MediaError, 'padding.*大於 0'):
                recognize(Path('unused'), padding=0, gap=0)
            load.assert_not_called()
            inputs.assert_not_called()

    def test_direct_video_analysis_rejects_invalid_padding_before_decoder_access(self):
        class Engine:
            @property
            def cv(self):
                raise AssertionError('invalid padding must not access the decoder')
        for padding in (0, -1, float('nan'), float('inf')):
            with self.subTest(padding=padding):
                with self.assertRaisesRegex(MediaError, 'padding.*大於 0'):
                    analyze_video(Path('unused'), Engine(), [[1, 0]], 0.5, 0.5, padding, 0)

    def test_direct_video_analysis_rejects_invalid_gap_before_decoder_access(self):
        class Engine:
            @property
            def cv(self):
                raise AssertionError('invalid gap must not access the decoder')
        for gap in (-1, float('nan'), float('inf')):
            with self.subTest(gap=gap):
                with self.assertRaisesRegex(MediaError, 'gap.*不小於 0'):
                    analyze_video(Path('unused'), Engine(), [[1, 0]], 0.5, 0.5, 2, gap)

    def test_reference_folder_excludes_notes_and_symlink_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            refs = root / 'data/reference-child'
            refs.mkdir(parents=True)
            (refs / 'kid.JPG').write_bytes(b'reference')
            (refs / 'README.md').write_text('instructions')
            (refs / '.hidden.jpg').write_bytes(b'not a reference')
            outside = root / 'elsewhere.jpg'
            outside.write_bytes(b'outside')
            (refs / 'linked.jpg').symlink_to(outside)
            self.assertEqual([p.name for p in reference_files(root)], ['kid.JPG'])

    def test_missing_reference_and_model_are_distinct_actionable_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(MediaError, '參考照片'):
                check_inputs(root)
            refs = root / 'data/reference-child'
            refs.mkdir(parents=True)
            (refs / 'kid.jpg').write_bytes(b'reference')
            with self.assertRaisesRegex(MediaError, '模型'):
                check_inputs(root)

    def test_cosine_ignores_vector_magnitude_and_rejects_invalid_features(self):
        self.assertAlmostEqual(cosine_similarity([2, 0], [1, 0]), 1)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0)
        for a, b in [([0, 0], [1, 0]), ([float('nan'), 1], [1, 0]), ([1], [1, 2])]:
            with self.assertRaises(MediaError):
                cosine_similarity(a, b)

    def test_cosine_stays_correct_when_finite_vector_norms_would_overflow_or_underflow(self):
        self.assertAlmostEqual(cosine_similarity([1e300, 0], [-1e300, 0]), -1)
        self.assertAlmostEqual(cosine_similarity([1e-300, 0], [1e-300, 0]), 1)

    def test_photo_candidate_requires_similarity_threshold(self):
        class Engine:
            def __init__(self, values): self.values = values
            def describe_file(self, path): return self.values
        positive = analyze_image(Path('unused'), Engine([[1, 0]]), [[1, 0]], 0.5)
        self.assertEqual(positive['state'], 'candidate')
        self.assertEqual(positive['faceCount'], 1)
        negative = analyze_image(Path('unused'), Engine([[0, 1]]), [[1, 0]], 0.5)
        self.assertEqual(negative['state'], 'no_match')
        empty = analyze_image(Path('unused'), Engine([]), [[1, 0]], 0.5)
        self.assertEqual(empty['state'], 'no_face')
        self.assertIsNone(empty['score'])
        self.assertEqual(empty['faceCount'], 0)

    def test_direct_photo_analysis_rejects_invalid_threshold_before_decoding(self):
        class Engine:
            def describe_file(self, path):
                raise AssertionError('invalid threshold must not decode private photos')
        for threshold in (0, -1, 1.01, float('nan'), float('inf'), True, '0.5', None):
            with self.subTest(threshold=threshold):
                with self.assertRaisesRegex(MediaError, 'threshold'):
                    analyze_image(Path('unused'), Engine(), [[1, 0]], threshold)

    def negative_profile(self, features, image_hashes=()):
        return {'features': features, 'imageHashes': set(image_hashes),
                'exampleCount': 1, 'faceCount': len(features), 'fingerprint': 'synthetic'}

    def test_feedback_suppresses_known_wrong_face_without_exposing_features(self):
        class Engine:
            def describe_file(self, path): return [[0.8, 0.6]]
        profile = self.negative_profile([[0.8, 0.6]])
        self.assertEqual(analyze_image(Path('unused'), Engine(), [[1, 0]], 0.5)['state'], 'candidate')
        result = analyze_image(Path('unused'), Engine(), [[1, 0]], 0.5, profile)
        self.assertEqual(result['state'], 'no_match')
        self.assertEqual(result['reason'], 'negative_example')
        self.assertEqual(result['negativeRejectedFaceCount'], 1)
        self.assertEqual(result['negativeExampleCount'], 1)
        self.assertNotIn('features', json.dumps(result))
        self.assertNotIn('imageHashes', json.dumps(result))

    def test_feedback_preserves_true_child_and_group_with_other_rejected_face(self):
        class Engine:
            def __init__(self, faces): self.faces = faces
            def describe_file(self, path): return self.faces
        profile = self.negative_profile([[0.8, 0.6]])
        child = analyze_image(Path('unused'), Engine([[1, 0]]), [[1, 0]], 0.5, profile)
        group = analyze_image(Path('unused'), Engine([[0.8, 0.6], [1, 0]]), [[1, 0]], 0.5, profile)
        self.assertEqual(child['state'], 'candidate')
        self.assertEqual(child['negativeRejectedFaceCount'], 0)
        self.assertEqual(group['state'], 'candidate')
        self.assertEqual(group['negativeRejectedFaceCount'], 1)
        self.assertEqual(group['score'], 1)

    def test_feedback_requires_negative_advantage_and_exact_rejected_bytes_stay_excluded(self):
        class Engine:
            def describe_file(self, path): return [[1, 0]]
        profile = self.negative_profile([[1, 0]])
        result = analyze_image(Path('unused'), Engine(), [[1, 0]], 0.5, profile)
        self.assertEqual(result['state'], 'candidate', 'equal positive/negative similarity must not veto a face')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'copy.jpg'
            path.write_bytes(b'explicitly-rejected-original')
            profile['imageHashes'] = {hashlib.sha256(path.read_bytes()).hexdigest()}
            exact = analyze_image(path, Engine(), [[1, 0]], 0.5, profile)
        self.assertEqual(exact['state'], 'no_match')
        self.assertEqual(exact['reason'], 'negative_example')

    def test_feedback_photo_identity_rejects_other_resolution_only_for_same_photo(self):
        class Engine:
            def describe_file(self, path): return [[1, 0]]
        profile = self.negative_profile([[0.8, 0.6]])
        profile['photoIds'] = {'123'}
        rejected = analyze_image(Path('unused'), Engine(), [[1, 0]], 0.5, profile, photo_id='123')
        other = analyze_image(Path('unused'), Engine(), [[1, 0]], 0.5, profile, photo_id='124')
        self.assertEqual(rejected['state'], 'no_match')
        self.assertEqual(rejected['reason'], 'negative_example')
        self.assertEqual(other['state'], 'candidate')

    def write_negative_example(self, root, excluded=True):
        content = b'\xff\xd8\xff\xe0synthetic-negative'
        digest = hashlib.sha256(content).hexdigest()
        relative = 'negative-examples/' + digest + '.jpg'
        destination = root / 'data' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        body = {'version': 1, 'revision': 1, 'examples': {'asset-1': {
            'postId': 'post-1', 'assetId': 'asset-1', 'excluded': excluded,
            'imageHash': digest, 'path': relative, 'updatedAt': '2026-11-01T12:00:00+00:00'}}}
        (root / 'data/feedback.json').write_text(json.dumps(body))
        return destination, body

    def test_private_negative_profile_and_fingerprint_disable_restored_examples(self):
        module = importlib.import_module('recognize')
        loader = getattr(module, 'load_negative_profile', None)
        fingerprinter = getattr(module, 'feedback_fingerprint', None)
        self.assertTrue(callable(loader) and callable(fingerprinter))
        class Engine:
            def describe_file(self, path): return [[0.8, 0.6]]
            def describe_bytes(self, content): return [[0.8, 0.6]]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(fingerprinter(root), '')
            path, body = self.write_negative_example(root)
            profile = loader(root, Engine())
            self.assertEqual(profile['features'], [[0.8, 0.6]])
            self.assertEqual(profile['exampleCount'], 1)
            self.assertEqual(profile['faceCount'], 1)
            self.assertEqual(profile['fingerprint'], fingerprinter(root))
            body['examples']['asset-1']['excluded'] = False
            (root / 'data/feedback.json').write_text(json.dumps(body))
            restored = loader(root, Engine())
            self.assertEqual(restored['features'], [])
            self.assertEqual(restored['imageHashes'], set())
            self.assertEqual(restored['fingerprint'], '')
            self.assertEqual(fingerprinter(root), '')

    def test_negative_profile_fails_safely_for_missing_or_modified_private_copy(self):
        module = importlib.import_module('recognize')
        loader = getattr(module, 'load_negative_profile', None)
        self.assertTrue(callable(loader))
        class Engine:
            def describe_file(self, path): return [[0.8, 0.6]]
            def describe_bytes(self, content): return [[0.8, 0.6]]
        for change in ('missing', 'modified', 'symlink'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                path, _ = self.write_negative_example(root)
                if change == 'modified':
                    path.write_bytes(b'changed')
                else:
                    path.unlink()
                    if change == 'symlink':
                        outside = root / 'private-original.jpg'
                        outside.write_bytes(b'do-not-read')
                        path.symlink_to(outside)
                with self.assertRaises(MediaError) as error:
                    loader(root, Engine())
                self.assertNotIn(path.name, str(error.exception))

    def test_negative_profile_unions_photo_ids_for_duplicate_image_and_binds_them_to_cache(self):
        module = importlib.import_module('recognize')
        class Engine:
            def describe_file(self, path): return [[0.8, 0.6]]
            def describe_bytes(self, content): return [[0.8, 0.6]]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, body = self.write_negative_example(root)
            body['examples']['asset-1']['photoId'] = '123'
            body['examples']['asset-2'] = {**body['examples']['asset-1'], 'assetId': 'asset-2', 'photoId': '124'}
            feedback_path = root / 'data/feedback.json'
            feedback_path.write_text(json.dumps(body))
            both = module.load_negative_profile(root, Engine())
            self.assertEqual(both['exampleCount'], 1)
            self.assertEqual(both['photoIds'], {'123', '124'})
            body['examples']['asset-2']['excluded'] = False
            feedback_path.write_text(json.dumps(body))
            single = module.load_negative_profile(root, Engine())
            self.assertEqual(single['photoIds'], {'123'})
            self.assertNotEqual(both['fingerprint'], single['fingerprint'])

    def test_negative_profile_decodes_the_same_verified_bytes_used_for_fingerprint(self):
        module = importlib.import_module('recognize')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, _ = self.write_negative_example(root)
            content = path.read_bytes()
            class Engine:
                def describe_file(self, unused):
                    raise AssertionError('negative descriptors must use the already verified bytes')
                def describe_bytes(self, value):
                    self.snapshot = value
                    path.write_bytes(b'concurrent-local-change')
                    return [[0.8, 0.6]]
            engine = Engine()
            profile = module.load_negative_profile(root, engine)
            self.assertEqual(engine.snapshot, content)
            self.assertEqual(profile['features'], [[0.8, 0.6]])
            self.assertEqual(profile['imageHashes'], {hashlib.sha256(content).hexdigest()})
            with self.assertRaises(MediaError):
                module.feedback_fingerprint(root)

    def test_photo_only_run_records_each_photo_and_separate_summary_counts(self):
        class Engine:
            def __init__(self, models): pass
            def describe_file(self, path):
                faces = {'reference.jpg': [[1, 0]], 'candidate.jpg': [[0, 1], [1, 0]],
                         'negative.jpg': [[0, 1]], 'empty.jpg': []}
                return faces[path.name]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            embedded = self.embed_post()
            video = {'id': 'old-local-video', 'kind': 'video',
                     'assets': [{'id': 'video-2', 'type': 'video', 'path': '../outside.mp4'}],
                     'recognition': {'state': 'pending', 'results': []}, 'clips': []}
            photos = {'id': 'local-photos', 'kind': 'photos', 'assets': [
                {'id': 'photo-1', 'type': 'image', 'path': 'media/local/candidate.jpg'},
                {'id': 'video-1', 'type': 'video', 'path': '../outside.mp4'},
                {'id': 'photo-2', 'type': 'image', 'path': 'media/local/negative.jpg'},
                {'id': 'photo-3', 'type': 'image', 'path': 'media/local/empty.jpg'}],
                'recognition': {'state': 'pending', 'results': []}, 'clips': []}
            manifest = load_manifest(root)
            manifest['posts'] = [embedded, video, photos]
            save_manifest(root, manifest)
            with patch('recognize.check_inputs', return_value=([root / 'reference.jpg'], [])), patch('recognize.FaceEngine', Engine):
                self.assertEqual(recognize(root, photos_only=True, sample_interval=float('nan'), padding=0, gap=-1), 1)
            posts = load_manifest(root)['posts']
            self.assertEqual(posts[:2], [embedded, video])
            recognition = posts[2]['recognition']
            self.assertEqual(recognition['state'], 'complete')
            self.assertEqual(recognition['referenceCount'], 1)
            self.assertEqual(recognition['threshold'], 0.5)
            self.assertEqual(recognition['counts'], {'inspected': 3, 'candidate': 1, 'noMatch': 1, 'noFace': 1})
            self.assertEqual([{key: result[key] for key in ('assetId', 'state', 'score', 'faceCount')}
                              for result in recognition['results']], [
                {'assetId': 'photo-1', 'state': 'candidate', 'score': 1.0, 'faceCount': 2},
                {'assetId': 'photo-2', 'state': 'no_match', 'score': 0.0, 'faceCount': 1},
                {'assetId': 'photo-3', 'state': 'no_face', 'score': None, 'faceCount': 0}])
            self.assertNotIn('sampleInterval', recognition)

    def test_reference_profile_excludes_outlier_using_positive_peer_agreement(self):
        from recognize import prepare_reference_profile
        references = [[1, 0], [1, 0.2], [1, -0.2], [0, 1]]
        profile = prepare_reference_profile(references, 0.5)
        self.assertEqual(profile['method'], 'reference_consensus_median')
        self.assertEqual(profile['referenceCount'], 4)
        self.assertEqual(profile['usableReferenceCount'], 3)
        self.assertEqual(profile['excludedReferenceCount'], 1)
        self.assertEqual(profile['usableReferenceIndices'], [0, 1, 2])
        # Each tilted positive has peer scores 5/sqrt(26) and 12/13.
        self.assertAlmostEqual(profile['effectiveThreshold'], (5 / (26 ** 0.5) + 12 / 13) / 2)
        self.assertEqual(len(profile['holdoutScores']), 3)

    def test_photo_consensus_accepts_positive_and_rejects_single_reference_similarity(self):
        class Engine:
            def __init__(self, faces): self.faces = faces
            def describe_file(self, path): return self.faces
        references = [[1, 0], [1, 0.2], [1, -0.2], [0, 1]]
        # This positive orientation is held out of the calibration references.
        positive = analyze_image(Path('unused'), Engine([[0.6, 0.8], [1, 0.1]]), references, 0.5)
        self.assertEqual(positive['state'], 'candidate')
        self.assertEqual(positive['faceCount'], 2)
        negative = analyze_image(Path('unused'), Engine([[0.6, 0.8]]), references, 0.5)
        self.assertEqual(negative['state'], 'no_match')
        self.assertAlmostEqual(positive['score'], 10 / (101 ** 0.5))
        self.assertEqual(positive['usableReferenceCount'], 3)
        self.assertEqual(positive['excludedReferenceCount'], 1)
        self.assertAlmostEqual(negative['score'], 0.6)
        self.assertLess(negative['score'], negative['effectiveThreshold'])

    def test_consensus_honors_higher_user_threshold_without_reselecting_references(self):
        class Engine:
            def describe_file(self, path): return [[1, 0]]
        references = [[1, 0], [1, 0.2], [1, -0.2], [0, 1]]
        result = analyze_image(Path('unused'), Engine(), references, 0.99)
        self.assertEqual(result['state'], 'no_match')
        self.assertEqual(result['effectiveThreshold'], 0.99)
        self.assertEqual(result['usableReferenceCount'], 3)

    def test_reference_margin_relaxes_calibration_and_preserves_higher_user_floor(self):
        class Engine:
            reference_threshold_margin = 0.015
            def describe_file(self, path): return [[0.99, (1 - 0.99 ** 2) ** 0.5]]
        references = [[1, 0], [1, 0], [1, 0], [0, 1]]
        candidate = analyze_image(Path('unused'), Engine(), references, 0.5)
        strict = analyze_image(Path('unused'), Engine(), references, 0.995)
        self.assertEqual(candidate['state'], 'candidate')
        self.assertAlmostEqual(candidate['effectiveThreshold'], 0.985)
        self.assertEqual(candidate['usableReferenceCount'], 3)
        self.assertEqual(candidate['excludedReferenceCount'], 1)
        self.assertEqual(strict['state'], 'no_match')
        self.assertEqual(strict['effectiveThreshold'], 0.995)

    def test_reference_margin_does_not_lower_floor_for_one_or_two_references(self):
        class Engine:
            reference_threshold_margin = 0.015
            def describe_file(self, path): return [[0.49, (1 - 0.49 ** 2) ** 0.5]]
        for count in (1, 2):
            with self.subTest(count=count):
                result = analyze_image(Path('unused'), Engine(), [[1, 0]] * count, 0.5)
                self.assertEqual(result['state'], 'no_match')
                self.assertEqual(result['effectiveThreshold'], 0.5)

    def test_reference_margin_keeps_negative_face_and_exact_identity_vetoes(self):
        face = [0.79, (1 - 0.79 ** 2) ** 0.5]
        class Engine:
            reference_threshold_margin = 0.015
            negative_profile = {'features': [face], 'imageHashes': set(), 'photoIds': {'100'}, 'exampleCount': 1}
            def describe_file(self, path): return [face]
        references = [[1, 0], [1, 0], [0.8, 0.6]]
        vetoed = analyze_image(Path('unused'), Engine(), references, 0.5)
        self.assertAlmostEqual(vetoed['effectiveThreshold'], 0.785)
        self.assertEqual(vetoed['state'], 'no_match')
        self.assertEqual(vetoed['reason'], 'negative_example')
        self.assertEqual(vetoed['negativeRejectedFaceCount'], 1)
        engine = Engine()
        engine.negative_profile = {'features': [], 'imageHashes': set(), 'photoIds': {'100'}, 'exampleCount': 1}
        exact = analyze_image(Path('unused'), engine, references, 0.5, photo_id='100')
        self.assertEqual(exact['state'], 'no_match')
        self.assertEqual(exact['reason'], 'negative_example')

    def test_reference_margin_config_defaults_and_rejects_unbounded_values(self):
        loader = getattr(importlib.import_module('recognize'), 'load_reference_threshold_margin', None)
        self.assertIsNotNone(loader, 'shared reference margin reader is missing')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(loader(root), 0)
            (root / 'config').mkdir()
            path = root / 'config/job.json'
            path.write_text('{}')
            self.assertEqual(loader(root), 0)
            path.write_text(json.dumps({'referenceThresholdMargin': 0.015}))
            self.assertEqual(loader(root), 0.015)
            for margin in (-0.001, 0.101, True, '0.015', None, float('nan'), float('inf')):
                with self.subTest(margin=margin):
                    path.write_text(json.dumps({'referenceThresholdMargin': margin}))
                    with self.assertRaises(MediaError):
                        loader(root)

    def test_standalone_photo_recognition_uses_persisted_reference_margin(self):
        class Engine:
            def __init__(self, models): pass
            def describe_file(self, path):
                return [[1, 0]] if path.name.startswith('reference') else [[0.99, (1 - 0.99 ** 2) ** 0.5]]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            (root / 'config/job.json').write_text(json.dumps({'referenceThresholdMargin': 0.015}))
            manifest = load_manifest(root)
            manifest['posts'] = [{'id': 'local-photo', 'kind': 'photos', 'clips': [],
                                  'assets': [{'id': 'photo-1', 'type': 'image', 'path': 'media/local/photo.jpg'}]}]
            save_manifest(root, manifest)
            references = [root / 'reference-{}.jpg'.format(index) for index in range(3)]
            with patch('recognize.check_inputs', return_value=(references, [])), patch('recognize.FaceEngine', Engine):
                recognize(root, photos_only=True)
            record = load_manifest(root)['posts'][0]['recognition']
            self.assertEqual(record['results'][0]['state'], 'candidate')
            self.assertAlmostEqual(record['effectiveThreshold'], 0.985)

    def test_inconsistent_multiple_references_fail_before_decoding_instead_of_false_success(self):
        class Engine:
            def describe_file(self, path):
                raise AssertionError('uncalibrated references must not decode photos')
        with self.assertRaisesRegex(MediaError, '參考照片.*一致'):
            analyze_image(Path('unused'), Engine(), [[1, 0, 0], [0, 1, 0], [0, 0, 1]], 0.5)

    def test_photo_only_run_without_images_preserves_manifest_and_skips_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = load_manifest(root)
            manifest['posts'] = [self.embed_post()]
            save_manifest(root, manifest)
            before = (root / 'data/manifest.json').read_bytes()
            with patch('recognize.check_inputs', side_effect=AssertionError('no images must not load models')):
                with self.assertRaisesRegex(MediaError, '照片'):
                    recognize(root, photos_only=True)
            self.assertEqual((root / 'data/manifest.json').read_bytes(), before)

    def test_reference_face_count_error_uses_index_instead_of_private_filename(self):
        class Engine:
            def __init__(self, models): pass
            def describe_file(self, path): return []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = load_manifest(root)
            manifest['posts'] = [{'id': 'photos', 'kind': 'photos', 'assets': [
                {'id': 'photo', 'type': 'image', 'path': 'media/photo.jpg'}]}]
            save_manifest(root, manifest)
            with patch('recognize.check_inputs', return_value=([root / 'private-reference.jpg'], [])), patch('recognize.FaceEngine', Engine):
                with self.assertRaises(MediaError) as raised:
                    recognize(root, photos_only=True)
            self.assertIn('第 1 張', str(raised.exception))
            self.assertNotIn('private-reference', str(raised.exception))

    def test_multiple_faces_use_best_match_without_claiming_confirmed_identity(self):
        score, count = match_features([[0, 1], [1, 0]], [[1, 0]])
        self.assertEqual(score, 1)
        self.assertEqual(count, 2)

    def test_video_intervals_use_decoded_timestamps_instead_of_frame_index(self):
        class Capture:
            def __init__(self): self.index = -1; self.released = False
            def isOpened(self): return True
            def get(self, key): return 10 if key == 1 else [0, 100, 600, 700, 2200][self.index]
            def read(self):
                self.index += 1
                return (True, [[0, 1]] if self.index == 0 else [[1, 0]]) if self.index < 5 else (False, None)
            def release(self): self.released = True
        capture = Capture()
        class CV:
            CAP_PROP_FPS = 1
            CAP_PROP_POS_MSEC = 2
            def VideoCapture(self, path): return capture
        class Engine:
            cv = CV()
            def describe(self, frame): return frame
        result = analyze_video(Path('unused'), Engine(), [[1, 0]], 0.5, 0.5, 0.2, 0)
        self.assertEqual(result['state'], 'candidate')
        self.assertAlmostEqual(result['duration'], 3.7)
        self.assertEqual(len(result['segments']), 2)
        self.assertAlmostEqual(result['segments'][0]['start'], 0.4)
        self.assertAlmostEqual(result['segments'][1]['start'], 2.0)
        self.assertTrue(capture.released)

    def test_video_with_unusable_timestamps_fails_without_invented_intervals(self):
        class Capture:
            def __init__(self): self.count = 0; self.released = False
            def isOpened(self): return True
            def get(self, key): return 10 if key == 1 else 0
            def read(self): self.count += 1; return (True, []) if self.count <= 2 else (False, None)
            def release(self): self.released = True
        capture = Capture()
        class CV:
            CAP_PROP_FPS = 1
            CAP_PROP_POS_MSEC = 2
            def VideoCapture(self, path): return capture
        class Engine:
            cv = CV()
            def describe(self, frame): return frame
        with self.assertRaisesRegex(MediaError, '時間戳'):
            analyze_video(Path('unused'), Engine(), [[1, 0]], 0.5, 0.5, 2, 2)
        self.assertTrue(capture.released)


if __name__ == '__main__':
    unittest.main()
