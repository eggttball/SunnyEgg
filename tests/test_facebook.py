"""Graph API boundary tests: fixtures stay inside private temporary test roots."""
import io
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common
import media
try:
    import facebook
except ModuleNotFoundError:
    facebook = None

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
ENV = {"FB_ACCESS_TOKEN": "test-secret-token", "FB_GRAPH_VERSION": "v99.0"}
JPEG = b"\xff\xd8\xff\xe0" + b"test fixture, not downloaded Facebook media"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"test fixture only"


def photo(url="https://scontent.test.fbcdn.net/photo.jpg?signature=private", identifier=None):
    identifier = identifier or str(int.from_bytes(hashlib.sha256(urlsplit(url).path.encode()).digest()[:8], 'big'))
    return {"type": "photo", "url": "https://www.facebook.com/photo.php?fbid=" + identifier,
            "target": {"id": identifier}, "media": {"image": {"src": url, "width": 640, "height": 480}}}


def video(identifier="987"):
    return {"type": "video_inline", "url": "https://www.facebook.com/watch/?v=" + identifier,
            "target": {"id": identifier}, "media": {"image": {"src": "https://scontent.test.fbcdn.net/preview.jpg"}}}


def post(identifier, attachment, date="2026-10-30T10:00:00+0000"):
    return {"id": "100063963161862_" + identifier, "message": "托嬰中心貼文",
            "created_time": date, "permalink_url": "https://www.facebook.com/example/posts/" + identifier,
            "attachments": {"data": attachment if isinstance(attachment, list) else [attachment]}}


class Response(io.BytesIO):
    def __init__(self, value, content_type="application/json"):
        super().__init__(json.dumps(value).encode() if isinstance(value, dict) else value)
        self.headers = {"Content-Type": content_type}


class FacebookTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(facebook, "official Facebook fetch implementation is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fetch(self, responses, **kwargs):
        requests = []
        def boundary(request, timeout=30):
            requests.append(request)
            if not responses:
                raise AssertionError("unexpected external request")
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        with mock.patch.object(facebook, "urlopen", side_effect=boundary):
            result = facebook.fetch(self.root, environ=ENV, now=NOW, **kwargs)
        return result, requests

    def seed_existing(self):
        path = self.root / "existing.jpg"
        path.write_bytes(JPEG)
        return media.import_media(self.root, "photos", "https://www.facebook.com/example/posts/existing", [path])

    def test_graph_rate_limit_codes_have_safe_distinct_reason(self):
        for code in [4, 17, 32, 613]:
            with self.subTest(code=code):
                failure = facebook.graph_failure({'code': code, 'is_transient': True, 'message': 'DO-NOT-LOG private URL'})
                self.assertEqual(failure.reason, 'rate_limit')
                self.assertEqual(failure.state, 'blocked')
                self.assertNotIn('DO-NOT-LOG', str(failure))

    def test_http_429_without_graph_error_is_a_rate_limit(self):
        failure = HTTPError('https://graph.facebook.com/v99.0/page', 429, 'rate limited', {}, io.BytesIO(b''))
        with mock.patch.object(facebook, 'urlopen', side_effect=failure):
            with self.assertRaises(facebook.FetchError) as caught:
                facebook.graph_json('https://graph.facebook.com/v99.0/page', 'unused')
        self.assertEqual(caught.exception.reason, 'rate_limit')

    def test_http_429_takes_precedence_over_parsed_graph_errors(self):
        for body in [{'error': {'code': 1}}, {'error': {}}, {'error': {'code': 190}}]:
            with self.subTest(body=body):
                failure = HTTPError('https://graph.facebook.com/v99.0/page', 429, 'rate limited', {},
                                    io.BytesIO(json.dumps(body).encode()))
                with mock.patch.object(facebook, 'urlopen', side_effect=failure):
                    with self.assertRaises(facebook.FetchError) as caught:
                        facebook.graph_json('https://graph.facebook.com/v99.0/page', 'unused')
                self.assertEqual(caught.exception.reason, 'rate_limit')

    def test_native_nested_album_is_flattened_in_order(self):
        album = {"type": "album", "subattachments": {"data": [photo(), {
            "type": "album", "subattachments": {"data": [photo("https://scontent.test.fbcdn.net/second.jpg")]}}]}}
        selected = facebook.select_posts([post("album", album)], NOW.timestamp() - 86400 * 30)
        self.assertEqual(selected["photos"]["photos"], ["https://scontent.test.fbcdn.net/photo.jpg?signature=private",
                                                         "https://scontent.test.fbcdn.net/second.jpg"])
        self.assertEqual(selected["photos"]["sourceId"], "100063963161862_album")

    def test_album_child_pages_import_all_photos_with_safe_bearer_requests(self):
        next_url = "https://graph.facebook.com/v99.0/album/subattachments?after=two&ACCESS_TOKEN=private&appsecret_proof=private"
        album = {"type": "album", "subattachments": {"data": [photo()], "paging": {"next": next_url}}}
        result, requests = self.fetch([
            Response({"data": [post("album", album), post("video", video())]}),
            Response({"data": [photo("https://scontent.test.fbcdn.net/second.jpg")]}),
            Response(JPEG, "image/jpeg"), Response(JPEG, "image/jpeg"),
            Response({"id": "987", "source": "https://video.test.fbcdn.net/native.mp4"}), Response(MP4, "video/mp4")])
        self.assertEqual(result["state"], "complete")
        posts = common.load_manifest(self.root)["posts"]
        self.assertEqual(len(posts[0]["assets"]), 2)
        self.assertTrue(all((self.root / "data" / asset["path"]).read_bytes() == JPEG for asset in posts[0]["assets"]))
        self.assertEqual(parse_qs(urlsplit(requests[1].full_url).query), {"after": ["two"]})
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer test-secret-token")
        self.assertIsNone(requests[2].get_header("Authorization"))
        self.assertIsNone(requests[3].get_header("Authorization"))

    def test_explicit_child_limit_recovers_photos_omitted_by_default_expansion(self):
        def boundary(request, timeout=30):
            query = parse_qs(urlsplit(request.full_url).query)
            if urlsplit(request.full_url).hostname == 'graph.facebook.com':
                fields = query.get('fields', [''])[0]
                count = 51 if 'attachments.limit(100)' in fields and 'subattachments.limit(100)' in fields else 12
                children = [photo('https://scontent.test.fbcdn.net/{}.jpg'.format(index)) for index in range(count)]
                return Response({'data': [post('album', {'type': 'album', 'subattachments': {'data': children}})]})
            return Response(JPEG, 'image/jpeg')
        with mock.patch.object(facebook, 'urlopen', side_effect=boundary):
            facebook.fetch(self.root, environ=ENV, now=NOW)
        self.assertEqual(len(common.load_manifest(self.root)['posts'][0]['assets']), 51)

    def test_terminal_edge_at_requested_limit_is_not_claimed_complete(self):
        with self.assertRaises(facebook.FetchError) as failure:
            facebook.complete_attachments({'data': [photo()] * 100}, 'unused', 'v99.0', 100)
        self.assertEqual(failure.exception.reason, 'pagination_limit')

    def test_declared_attachment_total_must_match_resolved_count(self):
        for total, reason in [(36, 'pagination_limit'), (0, 'invalid_response')]:
            with self.subTest(total=total):
                with self.assertRaises(facebook.FetchError) as failure:
                    facebook.complete_attachments({'data': [photo()] * 12, 'summary': {'total_count': total}},
                                                  'unused', 'v99.0', 100)
                self.assertEqual(failure.exception.reason, reason)

    def test_terminal_cursors_below_limit_preserve_count_evidence(self):
        complete = facebook.complete_attachments({'data': [photo()], 'paging': {'cursors': {'after': 'private-cursor'}},
                                                 'summary': {'total_count': 1}}, 'unused', 'v99.0', 100)
        proof = complete.get('completeness', {})
        self.assertEqual(proof.get('state'), 'complete')
        self.assertEqual(proof['itemCount'], 1)
        self.assertEqual(proof['totalCount'], 1)
        self.assertEqual(proof['requestedLimit'], 100)
        self.assertNotIn('private-cursor', json.dumps(proof))

    def test_full_first_page_with_next_and_short_terminal_page_is_complete(self):
        collection = {'data': [photo(identifier=str(index)) for index in range(100)], 'summary': {'total_count': 101},
                      'paging': {'next': 'https://graph.facebook.com/v99.0/album/subattachments?after=two'}}
        with mock.patch.object(facebook, 'graph_json', return_value={'data': [photo(identifier='100')]}):
            complete = facebook.complete_attachments(collection, 'unused', 'v99.0', 100)
        self.assertEqual(len(complete['data']), 101)
        self.assertEqual(complete.get('completeness', {}).get('pageCount'), 2)
        self.assertEqual(complete['completeness']['terminalPageCount'], 1)

    def test_overlapping_page_ids_cannot_satisfy_declared_total(self):
        for summary in [None, {'total_count': 3}]:
            with self.subTest(summary=summary):
                collection = {'data': [photo(identifier='100'), photo(identifier='101')],
                              'paging': {'next': 'https://graph.facebook.com/v99.0/album/subattachments?after=two'}}
                if summary is not None:
                    collection['summary'] = summary
                with mock.patch.object(facebook, 'graph_json', return_value={'data': [photo(identifier='101')]}):
                    with self.assertRaises(facebook.FetchError) as failure:
                        facebook.complete_attachments(collection, 'unused', 'v99.0', 100)
                self.assertEqual(failure.exception.reason, 'invalid_response')

    def test_top_attachment_later_video_prevents_photo_only_classification(self):
        mixed = post("mixed", photo())
        mixed["attachments"]["paging"] = {"next": "https://graph.facebook.com/v99.0/mixed/attachments?after=two"}
        result, _ = self.fetch([Response({"data": [mixed, post("video", video())]}),
                                Response({"data": [video("555")]}),
                                Response({"id": "987", "source": "https://video.test.fbcdn.net/native.mp4"}), Response(MP4, "video/mp4")])
        self.assertEqual((result["state"], result["reason"]), ("partial", "missing_post_type"))
        self.assertEqual([p["kind"] for p in common.load_manifest(self.root)["posts"]], ["video"])

    def test_nested_album_pagination_is_completed_recursively(self):
        nested = {"type": "album", "subattachments": {"data": [photo()], "paging": {
            "next": "https://graph.facebook.com/v99.0/nested/subattachments?after=two"}}}
        album = {"type": "album", "subattachments": {"data": [nested]}}
        result, _ = self.fetch([Response({"data": [post("album", album)]}),
                                Response({"data": [photo("https://scontent.test.fbcdn.net/second.jpg")]}),
                                Response(JPEG, "image/jpeg"), Response(JPEG, "image/jpeg")])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(len(common.load_manifest(self.root)["posts"][0]["assets"]), 2)

    def test_unsafe_child_paging_fails_before_following_or_gallery_mutation(self):
        existing = self.seed_existing()
        for next_url in ["http://graph.facebook.com/v99.0/a", "https://evil.test/v99.0/a",
                         "https://private@graph.facebook.com/v99.0/a", "https://graph.facebook.com/v88.0/a"]:
            with self.subTest(url=next_url):
                album = {"type": "album", "subattachments": {"data": [photo()], "paging": {"next": next_url}}}
                result, requests = self.fetch([Response({"data": [post("album", album)]})])
                self.assertEqual((result["state"], result["reason"]), ("error", "unsafe_url"))
                self.assertEqual(len(requests), 1)
                self.assertEqual(common.load_manifest(self.root)["posts"], [existing])

    def test_child_paging_cycle_fails_without_importing_incomplete_post(self):
        next_url = "https://graph.facebook.com/v99.0/album/subattachments?after=two"
        collection = {"data": [photo()], "paging": {"next": next_url + "&access_token=private"}}
        album = {"type": "album", "subattachments": collection}
        result, requests = self.fetch([Response({"data": [post("album", album)]}),
                                       Response({"data": [photo()], "paging": {"next": next_url}})])
        self.assertEqual((result["state"], result["reason"]), ("error", "pagination_cycle"))
        self.assertEqual(len(requests), 2)
        self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_child_page_bound_fails_instead_of_claiming_complete(self):
        album = {"type": "album", "subattachments": {"data": [photo()], "paging": {
            "next": "https://graph.facebook.com/v99.0/album/subattachments?after=two"}}}
        result, requests = self.fetch([Response({"data": [post("album", album), post("video", video())]})], max_pages=1)
        self.assertEqual((result["state"], result["reason"]), ("error", "pagination_limit"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_child_page_failure_preserves_existing_posts_and_files(self):
        existing = self.seed_existing()
        original = self.root / "data" / existing["assets"][0]["path"]
        album = {"type": "album", "subattachments": {"data": [photo()], "paging": {
            "next": "https://graph.facebook.com/v99.0/album/subattachments?after=two"}}}
        result, requests = self.fetch([Response({"data": [post("album", album)]}), URLError("private")])
        self.assertEqual((result["state"], result["reason"]), ("error", "remote_failure"))
        self.assertEqual(len(requests), 2)
        self.assertEqual(urlsplit(requests[1].full_url).hostname, "graph.facebook.com")
        self.assertEqual(common.load_manifest(self.root)["posts"], [existing])
        self.assertEqual(original.read_bytes(), JPEG)

    def test_selection_rejects_unresolved_attachment_pages(self):
        top = post("top", photo())
        top["attachments"]["paging"] = {"next": "https://graph.facebook.com/v99.0/top/attachments"}
        child = post("child", {"type": "album", "subattachments": {"data": [photo()], "paging": {
            "next": "https://graph.facebook.com/v99.0/child/subattachments"}}})
        self.assertEqual(facebook.select_posts([top, child], 0), {})

    def test_mixed_and_link_only_posts_are_rejected(self):
        link = {"type": "share", "url": "https://example.test/story", "media": {"image": {"src": "https://scontent.test.fbcdn.net/link.jpg"}}}
        selected = facebook.select_posts([post("mixed", [photo(), video()]), post("link", link),
                                           post("link-album", {"type": "share", "subattachments": {"data": [photo()]}})], 0)
        self.assertEqual(selected, {})

    def test_newest_each_kind_uses_actual_dates_not_feed_order(self):
        selected = facebook.select_posts([post("old-photo", photo(), "2026-10-20T10:00:00+0000"),
                                           post("video", video(), "2026-10-29T10:00:00+0000"),
                                           post("new-photo", photo(), "2026-10-30T10:00:00+0000")], 0)
        self.assertEqual(selected["photos"]["sourceUrl"], "https://www.facebook.com/example/posts/new-photo")
        self.assertEqual(selected["video"]["videoId"], "987")

    def test_api_permalink_secret_query_is_stripped_before_import_validation(self):
        item = post("photo", photo())
        item["permalink_url"] = "https://www.facebook.com/permalink.php?story_fbid=123&id=456&ACCESS_TOKEN=PRIVATE_MARKER&token=PRIVATE_MARKER"
        result, _ = self.fetch([Response({"data": [item]}), Response(JPEG, "image/jpeg")])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(common.load_manifest(self.root)["posts"][0]["sourceUrl"], "https://www.facebook.com/permalink.php?story_fbid=123&id=456")
        self.assertNotIn("PRIVATE_MARKER", json.dumps(common.load_manifest(self.root)))

    def test_missing_invalid_or_outside_lookback_dates_cannot_be_selected(self):
        items = [post("old", photo(), "2026-09-30T00:00:00+0000"), post("bad", photo(), "bad date"),
                 post("naive", photo(), "2026-10-30T00:00:00"), post("missing", photo(), None)]
        self.assertEqual(facebook.select_posts(items, 1790856000), {})

    def test_missing_token_or_version_blocks_before_any_network_and_preserves_posts(self):
        existing = self.seed_existing()
        for environ in [{}, {"FB_ACCESS_TOKEN": "private"}, {"FB_GRAPH_VERSION": "v99.0"}]:
            with self.subTest(environ=list(environ)), mock.patch.object(facebook, "urlopen", side_effect=AssertionError("network forbidden")):
                result = facebook.fetch(self.root, environ=environ, now=NOW)
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["reason"], "configuration")
            self.assertEqual(common.load_manifest(self.root)["posts"], [existing])

    def test_explicit_version_since_and_bearer_header_then_cdn_without_bearer(self):
        result, requests = self.fetch([Response({"data": [post("photo", photo()), post("video", video())]}),
                                       Response(JPEG, "image/jpeg"), Response({"id": "987", "source": "https://video.test.fbcdn.net/native.mp4?signature=private", "length": 3}),
                                       Response(MP4, "video/mp4")])
        self.assertEqual(result["state"], "complete")
        first = urlsplit(requests[0].full_url)
        self.assertEqual(first.path, "/v99.0/864287026968664/posts")
        query = parse_qs(first.query)
        self.assertEqual(query["since"], ["1790856000"])
        self.assertEqual(query["limit"], ["25"])
        self.assertIn("attachments.limit(100){type,url,target,media,subattachments.limit(100){type,url,target,media}}", query["fields"][0])
        self.assertNotIn("access_token", query)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-secret-token")
        self.assertIsNone(requests[1].get_header("Authorization"))
        self.assertIsNone(requests[3].get_header("Authorization"))
        posts = common.load_manifest(self.root)["posts"]
        self.assertEqual([p["kind"] for p in posts], ["photos", "video"])
        self.assertEqual(posts[0]["publishedAt"], "2026-10-30T10:00:00+00:00")
        self.assertEqual((self.root / "data" / posts[0]["assets"][0]["path"]).read_bytes(), JPEG)
        self.assertEqual((self.root / "data" / posts[1]["assets"][0]["path"]).read_bytes(), MP4)
        encoded = json.dumps(common.load_manifest(self.root))
        self.assertNotIn("test-secret-token", encoded)
        self.assertNotIn("signature=private", encoded)
        self.assertEqual(list((self.root / "data").glob(".facebook-*")), [])

    def test_safe_pagination_removes_tokens_and_newest_over_later_page_wins(self):
        next_url = "https://graph.facebook.com/v99.0/100063963161862/posts?after=abc&access_token=leaked&APPSECRET_PROOF=private"
        result, requests = self.fetch([Response({"data": [post("old", photo(), "2026-10-15T00:00:00+0000")], "paging": {"next": next_url}}),
                                       Response({"data": [post("new", photo())]}), Response(JPEG, "image/jpeg")])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(parse_qs(urlsplit(requests[1].full_url).query), {"after": ["abc"]})
        self.assertEqual(common.load_manifest(self.root)["posts"][0]["sourceUrl"], "https://www.facebook.com/example/posts/new")

    def test_max_pages_bounds_pagination(self):
        page = {"data": [], "paging": {"next": "https://graph.facebook.com/v99.0/page/posts?after=abc"}}
        result, requests = self.fetch([Response(page), Response(page)], max_pages=2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["state"], "partial")

    def test_permission_error_is_blocked_and_distinct_from_empty_feed(self):
        permission = HTTPError("https://graph.facebook.com/secret?access_token=test-secret-token", 400, "private", {},
                               io.BytesIO(json.dumps({"error": {"code": 200, "message": "private signed-url token"}}).encode()))
        blocked, _ = self.fetch([permission])
        empty, _ = self.fetch([Response({"data": []})])
        self.assertEqual((blocked["state"], blocked["reason"]), ("blocked", "permission"))
        self.assertEqual((empty["state"], empty["reason"]), ("partial", "no_matching_posts"))
        self.assertNotIn("signed-url", blocked["message"])
        self.assertNotIn("test-secret-token", blocked["message"])

    def test_fetch_failure_preserves_existing_manifest_posts(self):
        existing = self.seed_existing()
        result, _ = self.fetch([URLError("https://private.test?access_token=test-secret-token")])
        manifest = common.load_manifest(self.root)
        self.assertEqual(manifest["posts"], [existing])
        self.assertEqual(result["state"], "error")
        self.assertNotIn("test-secret-token", json.dumps(manifest))

    def test_unsafe_pagination_fails_without_following_or_importing(self):
        for next_url in ["http://graph.facebook.com/posts", "https://graph.facebook.com.evil.test/posts",
                         "https://user:private@graph.facebook.com/posts", "https://graph.facebook.com:8443/posts"]:
            with self.subTest(url=next_url):
                result, requests = self.fetch([Response({"data": [post("photo", photo())], "paging": {"next": next_url}})])
                self.assertEqual(result["state"], "error")
                self.assertEqual(len(requests), 1)
                self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_unsafe_source_url_does_not_download_or_import(self):
        for url in ["http://video.test.fbcdn.net/asset.mp4", "https://fbcdn.net.evil.test/asset.mp4",
                    "https://user:password@video.test.fbcdn.net/asset.mp4", "https://video.test.fbcdn.net:8443/asset.mp4"]:
            with self.subTest(url=url):
                result, requests = self.fetch([Response({"data": [post("video", video())]}), Response({"id": "987", "source": url})])
                self.assertEqual(result["state"], "error")
                self.assertEqual(len(requests), 2)
                self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_missing_video_source_is_unavailable_without_fake_import(self):
        result, requests = self.fetch([Response({"data": [post("video", video())]}), Response({"id": "987", "length": 3})])
        self.assertEqual((result["state"], result["reason"]), ("partial", "video_unavailable"))
        self.assertEqual(len(requests), 2)
        self.assertEqual(common.load_manifest(self.root)["posts"], [])

    def test_photo_import_survives_video_source_unavailable_as_partial(self):
        result, _ = self.fetch([Response({"data": [post("photo", photo()), post("video", video())]}),
                                Response(JPEG, "image/jpeg"), Response({"id": "987"})])
        self.assertEqual(result["state"], "partial")
        self.assertEqual([p["kind"] for p in common.load_manifest(self.root)["posts"]], ["photos"])

    def test_failed_native_download_never_imports_incomplete_album(self):
        existing = self.seed_existing()
        album = {"type": "album", "subattachments": {"data": [photo(), photo("https://scontent.test.fbcdn.net/second.jpg")]}}
        result, _ = self.fetch([Response({"data": [post("album", album)]}), Response(JPEG, "image/jpeg"), URLError("private download URL")])
        self.assertEqual(result["state"], "error")
        self.assertEqual(common.load_manifest(self.root)["posts"], [existing])
        self.assertEqual(list((self.root / "data").glob(".facebook-*")), [])

    def test_rerun_deduplicates_source_identity_without_redownloading(self):
        feed = {"data": [post("photo", photo()), post("video", video())]}
        self.fetch([Response(feed), Response(JPEG, "image/jpeg"), Response({"id": "987", "source": "https://video.test.fbcdn.net/native.mp4"}), Response(MP4, "video/mp4")])
        original = common.load_manifest(self.root)["posts"]
        result, requests = self.fetch([Response(feed)])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(len(requests), 1)
        self.assertEqual(common.load_manifest(self.root)["posts"], original)

    def test_redirect_denies_unsafe_destination_before_credentials_can_leave_graph(self):
        handler = facebook.SafeRedirectHandler()
        request = Request("https://graph.facebook.com/v99.0/posts", headers={"Authorization": "Bearer private"})
        for url in ["https://evil.test/steal", "http://graph.facebook.com/posts", "https://graph.facebook.com:8443/posts"]:
            with self.subTest(url=url), self.assertRaises(facebook.FetchError):
                handler.redirect_request(request, None, 302, "redirect", {}, url)

    def test_cdn_redirect_stays_https_allowlist_without_authorization(self):
        handler = facebook.SafeRedirectHandler()
        redirected = handler.redirect_request(Request("https://scontent.test.fbcdn.net/a.jpg"), None, 302, "redirect", {},
                                              "https://video.test.fbcdn.net/b.jpg?private=signature")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(facebook.FetchError):
            handler.redirect_request(Request("https://scontent.test.fbcdn.net/a.jpg"), None, 302, "redirect", {}, "https://evil.test/a.jpg")

    def test_rerun_reacquires_deleted_video_and_keeps_one_source_entry(self):
        feed = {"data": [post("photo", photo()), post("video", video())]}
        self.fetch([Response(feed), Response(JPEG, "image/jpeg"),
                    Response({"id": "987", "source": "https://video.test.fbcdn.net/native.mp4"}), Response(MP4, "video/mp4")])
        before = common.load_manifest(self.root)["posts"]
        (self.root / "data" / before[1]["assets"][0]["path"]).unlink()
        result, requests = self.fetch([Response(feed), Response({"id": "987", "source": "https://video.test.fbcdn.net/native.mp4"}),
                                       Response(MP4, "video/mp4")])
        self.assertEqual(result["state"], "complete")
        posts = common.load_manifest(self.root)["posts"]
        self.assertEqual(posts[0], before[0])
        self.assertEqual(len(posts), 2)
        self.assertEqual(len([p for p in posts if p["sourceIdentity"] == "facebook:100063963161862_video"]), 1)
        self.assertTrue((self.root / "data" / posts[1]["assets"][0]["path"]).is_file(), "complete reported without an original video")
        self.assertEqual((self.root / "data" / posts[1]["assets"][0]["path"]).read_bytes(), MP4)
        self.assertEqual(len(requests), 3)

    def test_rerun_reacquires_corrupt_photo_original(self):
        feed = {"data": [post("photo", photo())]}
        self.fetch([Response(feed), Response(JPEG, "image/jpeg")])
        first = common.load_manifest(self.root)["posts"][0]
        old_path = self.root / "data" / first["assets"][0]["path"]
        old_path.write_bytes(b"invalid image")
        result, requests = self.fetch([Response(feed), Response(JPEG, "image/jpeg")])
        repaired = common.load_manifest(self.root)["posts"][0]
        self.assertEqual(result["state"], "partial")
        self.assertEqual(len(requests), 2)
        self.assertEqual((self.root / "data" / repaired["assets"][0]["path"]).read_bytes(), JPEG)
        self.assertEqual(old_path.read_bytes(), b"invalid image")

    def assert_failed_video_repair(self, failure, reason):
        feed = {"data": [post("photo", photo()), post("video", video())]}
        self.fetch([Response(feed), Response(JPEG, "image/jpeg"),
                    Response({"id": "987", "source": "https://video.test.fbcdn.net/native.mp4"}), Response(MP4, "video/mp4")])
        original = common.load_manifest(self.root)["posts"]
        video_path = self.root / "data" / original[1]["assets"][0]["path"]
        video_path.unlink()
        result, requests = self.fetch([Response(feed), Response({"id": "987", "source": "https://video.test.fbcdn.net/native.mp4"}), failure])
        self.assertEqual((result["state"], result["reason"]), ("partial", reason))
        self.assertEqual(common.load_manifest(self.root)["posts"], original)
        self.assertEqual((self.root / "data" / original[0]["assets"][0]["path"]).read_bytes(), JPEG)
        self.assertFalse(video_path.exists())
        self.assertEqual(len(requests), 3)

    def test_failed_deleted_video_download_repair_never_reports_complete(self):
        self.assert_failed_video_repair(URLError("private failure"), "download_failure")

    def test_failed_deleted_video_import_repair_never_reports_complete(self):
        self.assert_failed_video_repair(Response(b"not MP4", "video/mp4"), "import_failure")

    def test_bad_version_and_nonpositive_bounds_cannot_make_requests(self):
        for options in [{"environ": {**ENV, "FB_GRAPH_VERSION": "latest"}}, {"lookback_days": 0}, {"max_pages": 0}]:
            with self.subTest(options=options), mock.patch.object(facebook, "urlopen", side_effect=AssertionError("network forbidden")):
                result = facebook.fetch(self.root, now=NOW, **({"environ": ENV, **options}))
                self.assertEqual(result["state"], "blocked")

    def test_explicit_env_file_literals_are_never_shell_expanded(self):
        path = self.root / ".env"
        path.write_text("# local fixture\nFB_ACCESS_TOKEN='${HOME}`whoami`$(whoami)'\nFB_GRAPH_VERSION=\"v99.0\"\nFB_PAGE_ID=123\n")
        self.assertTrue(hasattr(facebook, "load_environment_file"), "explicit literal-only configuration is missing")
        loaded = facebook.load_environment_file(path)
        self.assertEqual(loaded, {"FB_ACCESS_TOKEN": "${HOME}`whoami`$(whoami)", "FB_GRAPH_VERSION": "v99.0", "FB_PAGE_ID": "123"})

    def test_app_credentials_are_literal_and_not_sent_by_photo_fetch(self):
        path = self.root / ".env"
        path.write_text("FB_ACCESS_TOKEN=test-secret-token\nFB_GRAPH_VERSION=v99.0\nFB_PAGE_ID=123\n"
                        "FB_APP_ID=456\nFB_APP_SECRET='${HOME}`whoami`$(whoami)'\n")
        try:
            loaded = facebook.load_environment_file(path)
        except facebook.FetchError:
            self.fail('Explicit environment files must accept the two private App credential keys')
        self.assertEqual(loaded['FB_APP_ID'], '456')
        self.assertEqual(loaded['FB_APP_SECRET'], '${HOME}`whoami`$(whoami)')
        requests = []
        def boundary(request, timeout=30):
            requests.append(request)
            return Response({"data": []})
        with mock.patch.object(facebook, "urlopen", side_effect=boundary):
            result = facebook.fetch(self.root, environ={}, now=NOW, env_file=path)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer test-secret-token')
        self.assertNotIn('client_secret', requests[0].full_url)
        self.assertNotIn(loaded['FB_APP_SECRET'], requests[0].full_url)
        self.assertNotIn(loaded['FB_APP_SECRET'], json.dumps(result))

    def test_exported_environment_overrides_explicit_file(self):
        self.assertTrue(hasattr(facebook, "load_environment_file"), "explicit literal-only configuration is missing")
        path = self.root / ".env"
        path.write_text("FB_ACCESS_TOKEN=file-only-private\nFB_GRAPH_VERSION=v88.0\nFB_PAGE_ID=123\n")
        requests = []
        def boundary(request, timeout=30):
            requests.append(request)
            return Response({"data": []})
        with mock.patch.object(facebook, "urlopen", side_effect=boundary):
            result = facebook.fetch(self.root, environ=ENV, now=NOW, env_file=path)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-secret-token")
        self.assertEqual(urlsplit(requests[0].full_url).path, "/v99.0/123/posts")
        self.assertNotIn("file-only-private", json.dumps(common.load_manifest(self.root)))

    def test_invalid_explicit_env_file_blocks_without_network_or_secret_output(self):
        self.assertTrue(hasattr(facebook, "load_environment_file"), "explicit literal-only configuration is missing")
        valid = self.root / "valid.env"
        valid.write_text("FB_ACCESS_TOKEN=private\nFB_GRAPH_VERSION=v99.0\n")
        symlink = self.root / "symlink.env"
        symlink.symlink_to(valid)
        bad = self.root / "bad.env"
        for value in ["FB_ACCESS_TOKEN=private\x00secret", "SECRET=private", "FB_ACCESS_TOKEN='unterminated", "FB_ACCESS_TOKEN=private\nFB_ACCESS_TOKEN=other"]:
            bad.write_text(value)
            for path in [bad, symlink, self.root, self.root / "missing.env"]:
                with self.subTest(path=path.name, invalid=value[:10]), mock.patch.object(facebook, "urlopen", side_effect=AssertionError("network forbidden")):
                    result = facebook.fetch(self.root, environ={}, now=NOW, env_file=path)
                self.assertEqual((result["state"], result["reason"]), ("blocked", "configuration"))
                self.assertNotIn("private", result["message"])

    def test_cli_reads_only_explicit_local_file_and_imports_no_fixture_feed(self):
        path = self.root / ".env"
        path.write_text("FB_ACCESS_TOKEN=\nFB_GRAPH_VERSION=\n")
        result = subprocess.run([sys.executable, str(Path(facebook.__file__).resolve()), "--root", str(self.root),
                                 "fetch", "--env-file", str(path)], env={}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["state"], "blocked")
        self.assertIn("FB_ACCESS_TOKEN", result.stdout)
        self.assertEqual(common.load_manifest(self.root)["posts"], [])


if __name__ == "__main__":
    unittest.main()
