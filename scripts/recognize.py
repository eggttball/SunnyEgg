#!/usr/bin/env python3
"""Local candidate face matching. Never uploads images or downloads models."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from statistics import median
import sys

from common import (MediaError, finite_number, load_manifest, manifest_lock,
                    merge_segments, safe_data_path, save_manifest)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MODEL_NAMES = ('face_detection_yunet_2023mar.onnx', 'face_recognition_sface_2021dec.onnx')
REFERENCE_AGREEMENT_THRESHOLD = 0.5
NEGATIVE_MATCH_THRESHOLD = 0.75
NEGATIVE_MATCH_MARGIN = 0.05
NEGATIVE_IMAGE_LIMIT = 50 * 1024 * 1024


def load_reference_threshold_margin(root):
    path = Path(root) / 'config/job.json'
    if not path.exists():
        return 0.0
    try:
        with path.open(encoding='utf-8') as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError()
        margin = finite_number(config.get('referenceThresholdMargin', 0.0))
        if margin is None or not 0 <= margin <= 0.1:
            raise ValueError()
        return float(margin)
    except (OSError, ValueError, UnicodeError):
        raise MediaError('referenceThresholdMargin 必須是 0 到 0.1 的有限數值；請確認 config/job.json') from None


def reference_files(root):
    directory = safe_data_path(root, 'reference-child')
    if not directory.exists():
        return []
    return sorted(path for path in directory.iterdir()
                  if path.is_file() and not path.is_symlink() and not path.name.startswith('.')
                  and path.suffix.lower() in REFERENCE_EXTENSIONS)


def check_inputs(root):
    references = reference_files(root)
    if not references:
        raise MediaError('請先把孩子的參考照片放入 data/reference-child/（JPG、PNG 或 WebP）')
    models = [safe_data_path(root, 'models/' + name) for name in MODEL_NAMES]
    if any(not path.is_file() or path.stat().st_size < 1024 for path in models):
        raise MediaError('缺少本機 ONNX 模型，請依 data/models/README.md 放好 YuNet 與 SFace 模型')
    for model in models:
        with model.open('rb') as handle:
            if handle.read(100).startswith(b'version https://git-lfs.github.com'):
                raise MediaError('模型檔是 Git LFS 指標，請下載實際 ONNX 模型檔')
    return references, models


def cosine_similarity(a, b):
    if not a or len(a) != len(b):
        raise MediaError('人臉特徵長度不符')
    if any(finite_number(value) is None for value in list(a) + list(b)):
        raise MediaError('人臉特徵含有無效數值')
    scale_a = max(abs(value) for value in a)
    scale_b = max(abs(value) for value in b)
    if scale_a == 0 or scale_b == 0:
        raise MediaError('人臉特徵沒有可比對資訊')
    scaled_a = [value / scale_a for value in a]
    scaled_b = [value / scale_b for value in b]
    norm_a = math.sqrt(math.fsum(value * value for value in scaled_a))
    norm_b = math.sqrt(math.fsum(value * value for value in scaled_b))
    score = math.fsum((x / norm_a) * (y / norm_b) for x, y in zip(scaled_a, scaled_b))
    return max(-1.0, min(1.0, score))


def match_features(features, references):
    if not features:
        return None, 0
    if not references:
        raise MediaError('沒有可比對的參考人臉')
    score = max(cosine_similarity(face, reference) for face in features for reference in references)
    return score, len(features)


def prepare_reference_profile(references, threshold, reference_threshold_margin=0.0):
    """Calibrate only from reference peer agreement; never from target outcomes.

    The returned profile contains indices and scalar diagnostics, not descriptors.
    One or two references retain the legacy maximum-cosine matching contract.
    """
    if finite_number(threshold) is None or not 0 < threshold <= 1:
        raise MediaError('threshold 必須大於 0 且不超過 1')
    if finite_number(reference_threshold_margin) is None or not 0 <= reference_threshold_margin <= 0.1:
        raise MediaError('referenceThresholdMargin 必須是 0 到 0.1 的有限數值')
    if not references:
        raise MediaError('沒有可比對的參考人臉')
    for reference in references:
        cosine_similarity(reference, reference)
    count = len(references)
    usable = list(range(count))
    profile = {'method': 'best_reference_cosine', 'referenceCount': count,
               'usableReferenceCount': count, 'excludedReferenceCount': 0,
               'effectiveThreshold': float(threshold), 'usableReferenceIndices': usable,
               'holdoutScores': []}
    if reference_threshold_margin:
        profile['referenceThresholdMargin'] = float(reference_threshold_margin)
    if count < 3:
        return profile
    similarities = [[cosine_similarity(a, b) for b in references] for a in references]
    usable = [index for index, scores in enumerate(similarities)
              if sum(score >= REFERENCE_AGREEMENT_THRESHOLD for other, score in enumerate(scores)
                     if other != index) > (count - 1) / 2]
    if len(usable) < 3:
        raise MediaError('參考照片的人臉比對缺乏一致性；請提供至少 3 張彼此一致的清楚參考照片')
    holdouts = [median(similarities[index][other] for other in usable if other != index)
                for index in usable]
    profile.update({'method': 'reference_consensus_median', 'usableReferenceIndices': usable,
                    'usableReferenceCount': len(usable), 'excludedReferenceCount': count - len(usable),
                    'effectiveThreshold': max(float(threshold), min(holdouts) - reference_threshold_margin),
                    'holdoutScores': holdouts})
    return profile


def _negative_images(root):
    """Read active local labels and verify their immutable image copies."""
    message = '無法讀取本機誤判標記或負例照片；請確認本機資料完整後重試'
    try:
        feedback_path = safe_data_path(root, 'feedback.json')
        if not feedback_path.exists():
            return []
        from feedback import load_feedback
        body = load_feedback(root)
        active = []
        seen = {}
        for identifier, example in sorted(body['examples'].items()):
            if not example['excluded']:
                continue
            path = safe_data_path(root, example['path'], allowed={'negative-examples'})
            descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(descriptor, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= NEGATIVE_IMAGE_LIMIT:
                    raise ValueError()
                content = handle.read(NEGATIVE_IMAGE_LIMIT + 1)
            digest = hashlib.sha256(content).hexdigest()
            if len(content) > NEGATIVE_IMAGE_LIMIT or digest != example['imageHash']:
                raise ValueError()
            if digest not in seen:
                seen[digest] = {'content': content, 'imageHash': digest, 'photoIds': set()}
                active.append(seen[digest])
            if example.get('photoId'):
                seen[digest]['photoIds'].add(example['photoId'])
        return active
    except (MediaError, OSError, ValueError, TypeError, KeyError, ImportError):
        raise MediaError(message) from None


def _negative_fingerprint(images):
    if not images:
        return ''
    digest = hashlib.sha256()
    for image in sorted(images, key=lambda image: image['imageHash']):
        digest.update(image['imageHash'].encode('ascii'))
        digest.update(b'\0')
        for photo_id in sorted(image['photoIds']):
            digest.update(photo_id.encode('ascii'))
            digest.update(b'\0')
        digest.update(b'\0')
    return digest.hexdigest()


def feedback_fingerprint(root):
    """Only active, verified negative images affect future matching caches."""
    return _negative_fingerprint(_negative_images(root))


def extend_negative_profile(profile, previous_feedback, current_feedback):
    """Adopt only additive verified IDs; retain the original image/face snapshot."""
    def images(body):
        grouped = {}
        for example in body['examples'].values():
            if example['excluded']:
                image = grouped.setdefault(example['imageHash'], {'imageHash': example['imageHash'], 'photoIds': set()})
                if example.get('photoId'):
                    image['photoIds'].add(example['photoId'])
        return grouped
    before, after = images(previous_feedback), images(current_feedback)
    if (set(before) != profile['imageHashes'] or set(after) != set(before)
            or _negative_fingerprint(list(before.values())) != profile['fingerprint']
            or any(not image['photoIds'].issubset(after[digest]['photoIds']) for digest, image in before.items())):
        raise MediaError('誤判標記已變更，不能沿用原有負例辨識快照')
    return {**profile, 'photoIds': {photo_id for image in after.values() for photo_id in image['photoIds']},
            'fingerprint': _negative_fingerprint(list(after.values()))}


def load_negative_profile(root, engine):
    """Derive negative descriptors in memory; never persist or return them in audits."""
    images = _negative_images(root)
    features = []
    try:
        for image in images:
            for face in engine.describe_bytes(image['content']):
                cosine_similarity(face, face)
                if features:
                    cosine_similarity(face, features[0])
                features.append(face)
    except Exception:
        raise MediaError('本機負例照片無法辨識；請確認誤判標記的原始照片完整') from None
    return {'features': features, 'imageHashes': {image['imageHash'] for image in images},
            'photoIds': {photo_id for image in images for photo_id in image['photoIds']},
            'exampleCount': len(images), 'faceCount': len(features),
            'fingerprint': _negative_fingerprint(images)}


def analyze_image(path, engine, references, threshold, negative_profile=None, photo_id=None):
    profile = prepare_reference_profile(references, threshold, getattr(engine, 'reference_threshold_margin', 0.0))
    features = engine.describe_file(path)
    usable = [references[index] for index in profile['usableReferenceIndices']]
    scores = [max(cosine_similarity(face, reference) for reference in usable)
              if profile['method'] == 'best_reference_cosine'
              else median(cosine_similarity(face, reference) for reference in usable)
              for face in features]
    negatives = negative_profile if negative_profile is not None else getattr(engine, 'negative_profile', None)
    negative_features = negatives.get('features', []) if negatives else []
    eligible, rejected = [], 0
    for face, positive_score in zip(features, scores):
        negative_score = max((cosine_similarity(face, negative) for negative in negative_features), default=None)
        if (positive_score >= profile['effectiveThreshold'] and negative_score is not None
                and negative_score >= NEGATIVE_MATCH_THRESHOLD
                and negative_score >= positive_score + NEGATIVE_MATCH_MARGIN):
            rejected += 1
        else:
            eligible.append(positive_score)
    exact_rejection = bool(negatives and photo_id is not None and photo_id in negatives.get('photoIds', set()))
    if not exact_rejection and negatives and negatives.get('imageHashes'):
        try:
            exact_rejection = hashlib.sha256(Path(path).read_bytes()).hexdigest() in negatives['imageHashes']
        except OSError:
            raise MediaError('無法讀取待比對照片以核對本機誤判標記') from None
    count = len(features)
    score = max(eligible, default=max(scores, default=None))
    state = 'no_face' if not count else 'candidate' if eligible and score >= profile['effectiveThreshold'] and not exact_rejection else 'no_match'
    result = {'state': state,
            'score': score, 'faceCount': count,
            **{key: profile[key] for key in ('method', 'referenceCount', 'usableReferenceCount',
                                             'excludedReferenceCount', 'effectiveThreshold')}}
    if 'referenceThresholdMargin' in profile:
        result['referenceThresholdMargin'] = profile['referenceThresholdMargin']
    if negatives and negatives.get('exampleCount', 0):
        result.update({'negativeExampleCount': negatives['exampleCount'],
                       'negativeFaceCount': len(negative_features), 'negativeRejectedFaceCount': rejected})
    if exact_rejection or rejected and state == 'no_match':
        result['reason'] = 'negative_example'
    return result


class FaceEngine:
    def __init__(self, models):
        try:
            import cv2
        except ImportError as error:
            raise MediaError('缺少 OpenCV；請在虛擬環境安裝 requirements-optional.txt') from error
        self.cv = cv2
        try:
            self.detector = cv2.FaceDetectorYN.create(str(models[0]), '', (320, 320), 0.8, 0.3, 5000)
            self.recognizer = cv2.FaceRecognizerSF.create(str(models[1]), '')
        except (AttributeError, cv2.error) as error:
            raise MediaError('OpenCV 或 ONNX 模型無法載入，請確認套件版本與模型檔案') from error

    def describe(self, image):
        height, width = image.shape[:2]
        if width < 1 or height < 1:
            raise MediaError('圖片沒有有效尺寸')
        scale = min(1.0, 1280 / max(width, height))
        if scale < 1:
            image = self.cv.resize(image, (max(1, int(width * scale)), max(1, int(height * scale))))
        self.detector.setInputSize((image.shape[1], image.shape[0]))
        _, faces = self.detector.detect(image)
        if faces is None:
            return []
        return [self.recognizer.feature(self.recognizer.alignCrop(image, face)).flatten().tolist()
                for face in faces]

    def describe_file(self, path):
        image = self.cv.imread(str(path))
        if image is None:
            raise MediaError('照片無法解碼：{}'.format(path.name))
        return self.describe(image)

    def describe_bytes(self, content):
        """Decode the validated negative-image snapshot rather than rereading a file."""
        import numpy as np
        image = self.cv.imdecode(np.frombuffer(content, dtype=np.uint8), self.cv.IMREAD_COLOR)
        if image is None:
            raise MediaError('本機負例照片無法解碼')
        return self.describe(image)


def validate_segment_options(padding, gap):
    if finite_number(padding) is None or padding <= 0:
        raise MediaError('padding 必須是大於 0 的秒數')
    if finite_number(gap) is None or gap < 0:
        raise MediaError('gap 必須是不小於 0 的秒數')


def analyze_video(path, engine, references, threshold, sample_interval, padding, gap):
    validate_segment_options(padding, gap)
    cv = engine.cv
    capture = cv.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise MediaError('影片無法解碼：{}'.format(path.name))
    hits, best_score, face_count = [], None, 0
    next_sample, previous_time, last_delta, frame_count = 0.0, None, None, 0
    fps = finite_number(capture.get(cv.CAP_PROP_FPS))
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            timestamp = finite_number(capture.get(cv.CAP_PROP_POS_MSEC))
            if timestamp is None or timestamp < 0:
                raise MediaError('影片沒有可用時間戳，無法產生可靠剪輯區間')
            timestamp /= 1000.0
            frame_count += 1
            if previous_time is not None:
                if timestamp <= previous_time:
                    raise MediaError('影片時間戳無法遞增，請先以 FFmpeg 轉成可分析的 MP4')
                last_delta = timestamp - previous_time
            previous_time = timestamp
            if timestamp + 1e-6 < next_sample:
                continue
            score, count = match_features(engine.describe(frame), references)
            face_count += count
            if score is not None:
                best_score = max(best_score, score) if best_score is not None else score
                if score >= threshold:
                    hits.append({'time': timestamp, 'score': score})
            next_sample = timestamp + sample_interval
    finally:
        capture.release()
    if frame_count == 0:
        raise MediaError('影片沒有可解碼影格')
    tail = last_delta or (1.0 / fps if fps and fps > 0 else None)
    if tail is None:
        raise MediaError('無法確認影片時間範圍')
    duration = previous_time + tail
    segments = merge_segments(hits, duration, padding=padding, gap=gap)
    return {'state': 'candidate' if segments else 'no_match', 'score': best_score,
            'faceCount': face_count, 'duration': duration, 'sampleInterval': sample_interval,
            'segments': segments}


def recognize(root, post_id=None, threshold=0.5, sample_interval=0.5, padding=2, gap=2,
              photos_only=False):
    if finite_number(threshold) is None or not 0 < threshold <= 1:
        raise MediaError('threshold 必須大於 0 且不超過 1')
    if not photos_only:
        if finite_number(sample_interval) is None or sample_interval <= 0:
            raise MediaError('sample-interval 必須大於 0')
        validate_segment_options(padding, gap)
    posts = [post for post in load_manifest(root)['posts'] if post_id is None or post.get('id') == post_id]
    if not photos_only and post_id is not None and any(post.get('playback') == 'facebook_embed' for post in posts):
        raise MediaError('Facebook 內嵌影片不需要內容辨識，沒有可分析的本機影片素材')
    posts = [post for post in posts if post.get('playback') != 'facebook_embed']
    if photos_only:
        posts = [post for post in posts if any(asset.get('type') == 'image' for asset in post.get('assets', []))]
    if not posts:
        if photos_only:
            raise MediaError('尚未有可分析的本機照片素材')
        raise MediaError('尚未有可分析的本機素材；Facebook 內嵌影片不需要內容辨識')
    reference_threshold_margin = load_reference_threshold_margin(root)
    paths, models = check_inputs(root)
    engine = FaceEngine(models)
    engine.reference_threshold_margin = reference_threshold_margin
    references = []
    for index, path in enumerate(paths, 1):
        features = engine.describe_file(path)
        if len(features) != 1:
            raise MediaError('第 {} 張參考照片需只有一張清楚可辨識的人臉，目前偵測到 {} 張'.format(index, len(features)))
        references.append(features[0])
    profile = prepare_reference_profile(references, threshold, reference_threshold_margin)
    engine.negative_profile = load_negative_profile(root, engine)
    count = 0
    for post in posts:
        results = []
        counts = {'inspected': 0, 'candidate': 0, 'noMatch': 0, 'noFace': 0}
        for asset in post.get('assets', []):
            if photos_only and asset.get('type') != 'image':
                continue
            path = safe_data_path(root, asset.get('path'), allowed={'media'})
            if asset.get('type') == 'image':
                result = analyze_image(path, engine, references, threshold, photo_id=asset.get('sourcePhotoId'))
                counts['inspected'] += 1
                counts[{'candidate': 'candidate', 'no_match': 'noMatch', 'no_face': 'noFace'}[result['state']]] += 1
            elif asset.get('type') == 'video':
                result = analyze_video(path, engine, references, threshold, sample_interval, padding, gap)
            else:
                raise MediaError('不支援的素材類型')
            results.append({'assetId': asset['id'], **result})
        record = {'state': 'complete', 'message': '已以 {} 張參考照片完成候選比對；需要家長確認，未命中不代表孩子未出現。'.format(len(references)),
                  'processedAt': datetime.now(timezone.utc).isoformat(), 'referenceCount': len(references),
                  'threshold': threshold, 'counts': counts, 'results': results,
                  **{key: profile[key] for key in ('method', 'usableReferenceCount',
                                                   'excludedReferenceCount', 'effectiveThreshold')}}
        if 'referenceThresholdMargin' in profile:
            record['referenceThresholdMargin'] = profile['referenceThresholdMargin']
        if engine.negative_profile['exampleCount']:
            record.update(negativeExampleCount=engine.negative_profile['exampleCount'],
                          negativeFaceCount=engine.negative_profile['faceCount'])
        if not photos_only:
            record['sampleInterval'] = sample_interval
        with manifest_lock(root):
            manifest = load_manifest(root)
            current = next((item for item in manifest['posts'] if item.get('id') == post['id']), None)
            if current is None or current.get('assets') != post.get('assets'):
                raise MediaError('分析期間素材清單已變動，請重新執行')
            current['recognition'] = record
            current['clips'] = []  # Old clip records no longer reflect this matching run; files are not deleted.
            save_manifest(root, manifest)
        count += 1
        print('{}：完成候選比對'.format(post['id']))
    return count


def main():
    parser = argparse.ArgumentParser(description='以本機參考照片找出照片與影片中的候選人臉，無任何上傳')
    parser.add_argument('--post-id', help='只分析指定貼文；省略時分析全部')
    parser.add_argument('--threshold', type=float, default=0.5, help='餘弦相似度候選門檻，預設 0.5；須以實際幼兒素材校準')
    parser.add_argument('--photos-only', action='store_true', help='僅辨識本機照片，略過所有影片素材')
    parser.add_argument('--sample-interval', type=float, default=0.5, help='影片人臉取樣間隔秒數，預設 0.5')
    parser.add_argument('--padding', type=float, default=2, help='每次命中前後保留秒數，必須大於 0，預設 2')
    parser.add_argument('--gap', type=float, default=2, help='候選區間相隔少於此秒數則合併，須不小於 0，預設 2')
    args = parser.parse_args()
    try:
        recognize(ROOT, args.post_id, args.threshold, args.sample_interval, args.padding, args.gap,
                  photos_only=args.photos_only)
    except MediaError as error:
        print('辨識尚未完成：{}'.format(error), file=sys.stderr)
        return 2
    except Exception as error:
        print('本機辨識失敗（{}）；未成功的貼文不會寫入候選結果'.format(type(error).__name__), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
