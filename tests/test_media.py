"""Local fixtures exercise imports; external encoders/downloaders are isolated."""
import importlib
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    media = importlib.import_module("media")
    common = importlib.import_module("common")
except ModuleNotFoundError:
    media = common = None

JPEG = b"\xff\xd8\xff\xe0" + b"test-only-jpeg-fixture" + b"\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"test-only-video-fixture"
SOURCE = "https://www.facebook.com/example/posts/123456"


class LocalMediaTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(media, "The local media CLI has not been implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.photo = self.root / "input.jpg"
        self.photo.write_bytes(JPEG)
        self.video = self.root / "input.mp4"
        self.video.write_bytes(MP4)

    def import_photo(self):
        return media.import_media(self.root, "photos", SOURCE, [self.photo])

    def test_import_commit_guard_runs_under_lock_before_installing_any_media(self):
        def reject_commit():
            descriptor = os.open(str(self.root / 'data/.manifest.lock'), os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            raise common.MediaError('commit rejected by current feedback')
        with self.assertRaisesRegex(common.MediaError, 'commit rejected'):
            media.import_media(self.root, 'photos', SOURCE, [self.photo], before_commit=reject_commit)
        self.assertEqual(common.load_manifest(self.root)['posts'], [])
        self.assertFalse((self.root / 'data/media').exists())

    def require_embed(self):
        self.assertTrue(callable(getattr(media, "embed_video", None)), "Facebook embed recording is not implemented")
        return media.embed_video

    def test_embed_records_only_canonical_source_without_downloading(self):
        embed = self.require_embed()
        with mock.patch.object(media.subprocess, "run", side_effect=AssertionError("embed must not run downloader")), \
                mock.patch.object(media, "find_ytdlp", side_effect=AssertionError("embed must not inspect downloader")):
            post = embed(self.root, "https://m.facebook.com/reel/200000000000003/?mibextid=tracking#fragment", title="影片來源")
        self.assertEqual(common.load_manifest(self.root)["posts"], [post])
        self.assertEqual(post["sourceUrl"], "https://www.facebook.com/reel/200000000000003/")
        self.assertEqual(post["sourceIdentity"], "facebook-embed:200000000000003")
        self.assertEqual(post["kind"], "video")
        self.assertEqual(post["playback"], "facebook_embed")
        self.assertEqual(post["title"], "影片來源")
        self.assertIsNone(post["publishedAt"])
        self.assertEqual(post["assets"], [])
        self.assertEqual(post["clips"], [])
        self.assertEqual(post["recognition"]["state"], "not_required")
        self.assertEqual(post["recognition"]["results"], [])
        self.assertNotIn("iframe", json.dumps(post))
        self.assertNotIn("oEmbed", json.dumps(post))
        self.assertFalse((self.root / "data/media").exists())
        self.assertEqual(common.load_manifest(self.root)["fetchStatus"]["state"], "blocked")

    def test_embed_deduplicates_video_identity_across_explicit_url_forms(self):
        embed = self.require_embed()
        first = embed(self.root, "https://www.facebook.com/watch/?v=123&tracking=anything")
        second = embed(self.root, "https://web.facebook.com/example/videos/123/", published_at="2026-11-01T10:00:00+08:00")
        third = embed(self.root, "https://facebook.com/video.php?v=123")
        self.assertEqual(first["sourceUrl"], "https://www.facebook.com/watch/?v=123")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(third["id"], first["id"])
        self.assertEqual(len(common.load_manifest(self.root)["posts"]), 1)

    def test_regular_video_paths_are_canonical_watch_sources_for_frontend(self):
        embed = self.require_embed()
        cases = [("/videos/101/", "101"), ("/owner%20name/videos/102/", "102"), ("/小鹿/videos/103/", "103")]
        for path, video_id in cases:
            with self.subTest(path=path):
                recorded = embed(self.root, "https://www.facebook.com" + path + "?tracking=ignored")
                expected = "https://www.facebook.com/watch/?v=" + video_id
                self.assertEqual(recorded["sourceUrl"], expected)
                self.assertEqual(common.load_manifest(self.root)["posts"][-1]["sourceUrl"], expected)
                replayed = embed(self.root, expected)
                self.assertEqual(replayed["id"], recorded["id"])
        self.assertEqual(len(common.load_manifest(self.root)["posts"]), 3)

    def test_embed_rejects_short_links_invalid_video_ids_and_secret_queries(self):
        embed = self.require_embed()
        for url in ["https://www.facebook.com/share/v/1Eziv7Nytj/", "https://example.test/reel/123/",
                    "https://www.facebook.com/watch/?v=not-numeric", "https://www.facebook.com/reel/123/?ACCESS_TOKEN=private",
                    "https://www.facebook.com/watch/?v=123&oauth_token=private", "https://www.facebook.com/reel/abc/"]:
            with self.subTest(url=url), self.assertRaises(common.MediaError):
                embed(self.root, url)
        self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_embed_cli_preserves_confirmed_date_and_returns_the_record(self):
        script = str(Path(media.__file__).resolve())
        result = subprocess.run([sys.executable, "-B", script, "--root", str(self.root), "embed-video", "--url",
                                 "https://www.facebook.com/reel/123/", "--title", "來源影片", "--published-at", "2026-11-01T10:00:00+08:00"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        post = json.loads(result.stdout)
        self.assertEqual(post["publishedAt"], "2026-11-01T10:00:00+08:00")
        self.assertEqual(common.load_manifest(self.root)["posts"], [post])

    def test_import_copies_bytes_and_leaves_publication_date_unknown(self):
        post = self.import_photo()
        manifest = common.load_manifest(self.root)
        self.assertEqual(manifest["posts"], [post])
        self.assertIsNone(post["publishedAt"])
        self.assertEqual(post["recognition"]["state"], "pending")
        self.assertEqual((self.root / "data" / post["assets"][0]["path"]).read_bytes(), JPEG)
        self.assertTrue(self.photo.exists())
        self.assertNotEqual(post["id"], self.import_photo()["id"])
        self.assertEqual(len(common.load_manifest(self.root)["posts"]), 2)

    def test_source_identity_deduplicates_by_identity_or_permalink(self):
        try:
            post = media.import_media(self.root, "photos", SOURCE, [self.photo], source_id="facebook:page_123")
        except TypeError as error:
            self.fail("Source identity imports are not supported: {}".format(error))
        by_identity = media.import_media(self.root, "photos", "https://www.facebook.com/example/posts/alternate",
                                         [self.photo], source_id="facebook:page_123")
        by_permalink = media.import_media(self.root, "photos", SOURCE, [self.photo], source_id="facebook:alias")
        self.assertEqual(post["sourceIdentity"], "facebook:page_123")
        self.assertEqual(by_identity["id"], post["id"])
        self.assertEqual(by_permalink["id"], post["id"])
        self.assertEqual(len(common.load_manifest(self.root)["posts"]), 1)

    def test_identified_import_repairs_missing_original_without_duplicate_source(self):
        first = media.import_media(self.root, "photos", SOURCE, [self.photo], source_id="facebook:page_123")
        (self.root / "data" / first["assets"][0]["path"]).unlink()
        repaired = media.import_media(self.root, "photos", SOURCE, [self.photo], source_id="facebook:page_123")
        self.assertTrue((self.root / "data" / repaired["assets"][0]["path"]).is_file(), "repair left the original missing")
        self.assertEqual((self.root / "data" / repaired["assets"][0]["path"]).read_bytes(), JPEG)
        self.assertEqual(common.load_manifest(self.root)["posts"], [repaired])
        self.assertEqual(repaired["sourceIdentity"], "facebook:page_123")

    def test_identified_import_replaces_invalid_kind_assets_and_unsafe_originals(self):
        for invalid in ("kind", "assets", "type", "signature", "path", "symlink", "count"):
            with self.subTest(invalid=invalid):
                source = SOURCE + "?fbid=" + invalid
                first = media.import_media(self.root, "photos", source, [self.photo], source_id="facebook:" + invalid)
                manifest = common.load_manifest(self.root)
                original = self.root / "data" / first["assets"][0]["path"]
                bad = next(p for p in manifest["posts"] if p["id"] == first["id"])
                if invalid == "kind": bad["kind"] = "video"
                elif invalid == "assets": bad["assets"] = []
                elif invalid == "type": bad["assets"][0]["type"] = "video"
                elif invalid == "signature": original.write_bytes(b"broken JPEG")
                elif invalid == "path": bad["assets"][0]["path"] = "../input.jpg"
                elif invalid == "symlink": original.unlink(); original.symlink_to(self.photo)
                elif invalid == "count": bad["assets"].append(bad["assets"][0].copy())
                common.save_manifest(self.root, manifest)
                repaired = media.import_media(self.root, "photos", source, [self.photo], source_id="facebook:" + invalid)
                self.assertNotEqual(repaired["id"], first["id"])
                self.assertEqual((self.root / "data" / repaired["assets"][0]["path"]).read_bytes(), JPEG)
                self.assertEqual(len([p for p in common.load_manifest(self.root)["posts"] if p["sourceIdentity"] == "facebook:" + invalid]), 1)

    def test_failed_repair_preserves_manifest_and_other_original_files(self):
        first = media.import_media(self.root, "photos", SOURCE, [self.photo, self.photo], source_id="facebook:page_123")
        (self.root / "data" / first["assets"][0]["path"]).unlink()
        surviving = self.root / "data" / first["assets"][1]["path"]
        before = (self.root / "data/manifest.json").read_bytes()
        with mock.patch.object(common.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                media.import_media(self.root, "photos", SOURCE, [self.photo, self.photo], source_id="facebook:page_123")
        self.assertEqual((self.root / "data/manifest.json").read_bytes(), before)
        self.assertEqual(surviving.read_bytes(), JPEG)
        self.assertEqual(list((self.root / "data/media").iterdir()), [surviving.parent])

    def test_concurrent_identified_repairs_keep_one_available_source(self):
        first = media.import_media(self.root, "photos", SOURCE, [self.photo], source_id="facebook:page_123")
        (self.root / "data" / first["assets"][0]["path"]).unlink()
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import media; "
                "media.import_media(sys.argv[2], 'photos', sys.argv[3], [sys.argv[4]], source_id='facebook:page_123')")
        processes = [subprocess.Popen([sys.executable, "-c", code, str(Path(media.__file__).parent), str(self.root), SOURCE, str(self.photo)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(5)]
        outputs = [process.communicate(timeout=15) for process in processes]
        self.assertEqual([p.returncode for p in processes], [0] * 5, outputs)
        posts = common.load_manifest(self.root)["posts"]
        self.assertEqual(len(posts), 1)
        self.assertTrue((self.root / "data" / posts[0]["assets"][0]["path"]).is_file(), "concurrent repair left the original missing")
        self.assertEqual((self.root / "data" / posts[0]["assets"][0]["path"]).read_bytes(), JPEG)

    def test_import_rejects_wrong_kind_and_forged_signatures_without_writes(self):
        for kind, file in [("photos", self.video), ("video", self.photo)]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                media.import_media(self.root, kind, SOURCE, [file])
        self.photo.write_bytes(b"this is not a JPEG")
        with self.assertRaises(ValueError):
            self.import_photo()
        self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_import_rejects_unsafe_source_urls(self):
        urls = ["http://www.facebook.com/example/posts/1", "https://facebook.com.evil.test/posts/1",
                "https://user:secret@www.facebook.com/posts/1", "https://:@www.facebook.com/posts/1", "file:///etc/passwd",
                "https://www.facebook.com:8443/posts/1", "https://www.facebook.com/../posts/1"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                media.import_media(self.root, "photos", url, [self.photo])
        self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_import_rejects_secret_query_parameters_without_persisting_marker(self):
        original = self.import_photo()
        before = (self.root / "data/manifest.json").read_bytes()
        for key in ["AcCeSs_ToKeN", "APPSECRET_PROOF", "oauth_token", "token", "AUTHORIZATION", "%61ccess_token"]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "私密|權杖"):
                media.import_media(self.root, "photos", SOURCE + "?fbid=123&" + key + "=TEST_PRIVATE_MARKER", [self.photo])
        self.assertEqual((self.root / "data/manifest.json").read_bytes(), before)
        self.assertEqual(common.load_manifest(self.root)["posts"], [original])
        self.assertNotIn("TEST_PRIVATE_MARKER", json.dumps(common.load_manifest(self.root)))
        self.assertEqual(len(list((self.root / "data/media").glob("**/*.jpg"))), 1)

    def test_download_rejects_secret_query_before_external_execution_or_mutation(self):
        for key in ["ACCESS_TOKEN", "appsecret_proof", "OAuth_Token", "Token", "authorization"]:
            with self.subTest(key=key), mock.patch.object(media, "find_ytdlp", return_value=["/test/yt-dlp"]), \
                    mock.patch.object(media.subprocess, "run", side_effect=AssertionError("secret URL reached downloader")) as external:
                with self.assertRaisesRegex(ValueError, "私密|權杖"):
                    media.download_video(self.root, "https://www.facebook.com/watch/?v=123&" + key + "=TEST_PRIVATE_MARKER")
                external.assert_not_called()
        self.assertFalse((self.root / "data").exists())

    def test_manual_source_identity_query_parameters_remain_available(self):
        source = "https://www.facebook.com/permalink.php?story_fbid=123&id=456&fbid=789"
        imported = media.import_media(self.root, "photos", source, [self.photo])
        self.assertEqual(imported["sourceUrl"], source)
        self.assertEqual(media.validate_source_url("https://www.facebook.com/watch/?v=123", video=True), "https://www.facebook.com/watch/?v=123")

    def test_import_rejects_data_symlink_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "data").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.import_photo()
        self.assertEqual(list(outside.iterdir()), [])

    def test_import_rejects_private_reference_image_without_gallery_mutation(self):
        reference = self.root / "data/reference-child/child.jpg"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(JPEG)
        with self.assertRaisesRegex(ValueError, "參考照片"):
            media.import_media(self.root, "photos", SOURCE, [reference])
        self.assertFalse((self.root / "data/manifest.json").exists())
        self.assertFalse((self.root / "data/media").exists())
        self.assertEqual(reference.read_bytes(), JPEG)

    def test_import_rejects_reference_image_through_ancestor_symlink_alias(self):
        original = self.import_photo()
        before = (self.root / "data/manifest.json").read_bytes()
        reference = self.root / "data/reference-child/child.jpg"
        reference.parent.mkdir()
        reference.write_bytes(JPEG)
        alias = self.root / "alias"
        alias.symlink_to(self.root / "data", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "參考照片"):
            media.import_media(self.root, "photos", SOURCE, [alias / "reference-child/child.jpg"])
        self.assertEqual((self.root / "data/manifest.json").read_bytes(), before)
        self.assertEqual(common.load_manifest(self.root)["posts"], [original])
        self.assertEqual(len(list((self.root / "data/media").glob("**/*.jpg"))), 1)
        self.assertEqual(reference.read_bytes(), JPEG)

    def test_invalid_publication_date_does_not_create_post(self):
        with self.assertRaises(ValueError):
            media.import_media(self.root, "photos", SOURCE, [self.photo], published_at="yesterday")
        self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_failed_atomic_replace_preserves_previous_manifest(self):
        self.import_photo()
        previous = (self.root / "data/manifest.json").read_bytes()
        with mock.patch.object(common.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                common.save_manifest(self.root, {"version": 1, "posts": []})
        self.assertEqual((self.root / "data/manifest.json").read_bytes(), previous)

    def test_merge_segments_orders_merges_and_clamps_intervals(self):
        self.assertEqual(common.merge_segments([
            {"time": 9, "score": .8}, {"time": 1, "score": .5}, {"time": 4, "score": .9}
        ], 10), [{"start": 0.0, "end": 10.0, "score": .9}])
        self.assertEqual(common.merge_segments([{ "time": 1, "score": .7}, {"time": 12, "score": .8}],
                                               20, padding=1, gap=2),
                         [{"start": 0.0, "end": 2.0, "score": .7}, {"start": 11.0, "end": 13.0, "score": .8}])
        self.assertEqual(common.merge_segments([
            {"time": -1, "score": .8}, {"time": 11, "score": .8},
            {"time": math.nan, "score": .8}, {"time": 1, "score": math.inf}
        ], 10), [])

    def test_merge_segments_rejects_zero_padding_instead_of_discarding_hits(self):
        for hits in ([{"time": 1, "score": 1}], []):
            with self.subTest(hits=hits), self.assertRaisesRegex(ValueError, "padding.*大於 0"):
                common.merge_segments(hits, 2, padding=0, gap=0)
        self.assertEqual(common.merge_segments([{"time": 1, "score": 1}], 2, padding=.5, gap=0),
                         [{"start": .5, "end": 1.5, "score": 1.0}])

    def prepare_video(self):
        post = media.import_media(self.root, "video", "https://www.facebook.com/watch/?v=123", [self.video])
        manifest = common.load_manifest(self.root)
        manifest["posts"][0]["recognition"] = {"state": "complete", "message": "候選結果", "results": [{
            "assetId": post["assets"][0]["id"], "state": "candidate", "score": .8,
            "duration": 10, "sampleInterval": 1, "faceCount": 1,
            "segments": [{"start": 7, "end": 15, "score": .8}, {"start": -1, "end": 3, "score": .7}]
        }]}
        common.save_manifest(self.root, manifest)
        return post

    def test_clip_exports_clamped_candidate_intervals_with_optional_audio(self):
        post = self.prepare_video()
        commands = []
        def encode(argv, **kwargs):
            commands.append(argv)
            Path(argv[-1]).write_bytes(MP4)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(media, "find_ffmpeg", return_value="/test/ffmpeg"), \
                mock.patch.object(media.subprocess, "run", side_effect=encode):
            clips = media.clip_post(self.root, post["id"])
        self.assertEqual([(c["start"], c["end"]) for c in clips], [(0.0, 3.0), (7.0, 10.0)])
        self.assertEqual(len(common.load_manifest(self.root)["posts"][0]["clips"]), 2)
        self.assertTrue(all((self.root / "data" / clip["path"]).exists() for clip in clips))
        self.assertTrue(all("0:a?" in argv and "libx264" in argv and "-c:a" in argv for argv in commands))
        self.assertTrue(all(clip["state"] == "candidate" for clip in clips))

    def test_encoder_cannot_open_network_protocols_from_imported_video(self):
        post = self.prepare_video()
        def encode(argv, **kwargs):
            # Re-encoding a local MP4 must not enable remote references inside it.
            self.assertIn("-protocol_whitelist", argv)
            self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "file,pipe")
            self.assertLess(argv.index("-protocol_whitelist"), argv.index("-i"))
            Path(argv[-1]).write_bytes(MP4)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(media, "find_ffmpeg", return_value="/test/ffmpeg"), \
                mock.patch.object(media.subprocess, "run", side_effect=encode):
            self.assertEqual(len(media.clip_post(self.root, post["id"])), 2)

    def test_concurrent_cli_imports_do_not_overwrite_each_other(self):
        script = str(Path(media.__file__).resolve())
        processes = [subprocess.Popen([sys.executable, script, "--root", str(self.root), "import", "--kind", "photos",
                                       "--source-url", SOURCE, "--files", str(self.photo)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(5)]
        outputs = [process.communicate(timeout=15) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0, 0, 0, 0], outputs)
        posts = common.load_manifest(self.root)["posts"]
        self.assertEqual(len(posts), 5)
        self.assertEqual(len({post["id"] for post in posts}), 5)
        self.assertTrue(all((self.root / "data" / post["assets"][0]["path"]).read_bytes() == JPEG for post in posts))

    def test_concurrent_identified_imports_deduplicate_to_one_post(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import media; "
                "media.import_media(sys.argv[2], 'photos', sys.argv[3], [sys.argv[4]], source_id='facebook:page_123')")
        processes = [subprocess.Popen([sys.executable, "-c", code, str(Path(media.__file__).parent), str(self.root),
                                       SOURCE, str(self.photo)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(5)]
        outputs = [process.communicate(timeout=15) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0, 0, 0, 0], outputs)
        posts = common.load_manifest(self.root)["posts"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["sourceIdentity"], "facebook:page_123")

    def test_failed_encoder_preserves_manifest_and_removes_partial_clips(self):
        post = self.prepare_video()
        before = (self.root / "data/manifest.json").read_bytes()
        with mock.patch.object(media, "find_ffmpeg", return_value="/test/ffmpeg"), \
                mock.patch.object(media.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            with self.assertRaises(ValueError):
                media.clip_post(self.root, post["id"])
        self.assertEqual((self.root / "data/manifest.json").read_bytes(), before)
        self.assertEqual(list((self.root / "data/clips").glob("**/*.mp4")), [])

    def test_clip_rejects_manifest_paths_outside_media(self):
        post = self.prepare_video()
        manifest = common.load_manifest(self.root)
        manifest["posts"][0]["assets"][0]["path"] = "../input.mp4"
        common.save_manifest(self.root, manifest)
        with mock.patch.object(media, "find_ffmpeg", return_value="/test/ffmpeg"), self.assertRaises(ValueError):
            media.clip_post(self.root, post["id"])

    def test_download_requires_explicit_video_url_before_external_execution(self):
        with mock.patch.object(media.subprocess, "run") as external:
            with self.assertRaises(ValueError):
                media.download_video(self.root, "https://www.facebook.com/profile.php?id=123")
        external.assert_not_called()

    def test_failed_download_retains_posts_and_records_actual_failure(self):
        original = self.import_photo()
        with mock.patch.object(media, "find_ytdlp", return_value=["/test/yt-dlp"]), \
                mock.patch.object(media, "find_ffmpeg", return_value=None), \
                mock.patch.object(media.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "yt-dlp")):
            with self.assertRaises(ValueError):
                media.download_video(self.root, "https://www.facebook.com/watch/?v=123")
        manifest = common.load_manifest(self.root)
        self.assertEqual(manifest["posts"], [original])
        self.assertEqual(manifest["fetchStatus"]["state"], "failed")

    def test_successful_downloader_imports_only_generated_output_and_ignores_config(self):
        commands = []
        def download(argv, **kwargs):
            commands.append(argv)
            template = argv[argv.index("--output") + 1]
            Path(template.replace("%(ext)s", "mp4")).write_bytes(MP4)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(media, "find_ytdlp", return_value=["/test/yt-dlp"]), \
                mock.patch.object(media, "find_ffmpeg", return_value=None), \
                mock.patch.object(media.subprocess, "run", side_effect=download):
            post = media.download_video(self.root, "https://www.facebook.com/watch/?v=123")
        self.assertEqual((self.root / "data" / post["assets"][0]["path"]).read_bytes(), MP4)
        self.assertEqual(common.load_manifest(self.root)["fetchStatus"]["state"], "complete")
        self.assertIn("--ignore-config", commands[0])
        self.assertNotIn("--cookies-from-browser", commands[0])


if __name__ == "__main__":
    unittest.main()
