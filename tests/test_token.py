import importlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
NOW = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
EPOCH = int(NOW.timestamp())
DAY = 86400
OLD, NEW, SECRET = 'OLD_TOKEN_MARKER', 'NEW_TOKEN_MARKER', 'APP_SECRET_MARKER'
CONTENTS = ('# keep comments\r\nFB_APP_ID=123\r\nFB_APP_SECRET=' + SECRET +
            '\r\nFB_ACCESS_TOKEN = \'{}\'  \r\nFB_GRAPH_VERSION=v99.0\r\nFB_PAGE_ID=42\r\n').format(OLD).encode()


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode() if isinstance(body, dict) else body)
        self.headers = {'Content-Type': 'application/json'}


def debug(expires=EPOCH + 3600, **changes):
    data = {'is_valid': True, 'type': 'USER', 'app_id': '123', 'user_id': '999',
            'scopes': ['public_profile', 'pages_read_engagement'], 'expires_at': expires,
            'data_access_expires_at': EPOCH + 90 * DAY}
    data.update(changes)
    return {'data': data}


class TokenTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('facebook_token')
        except ModuleNotFoundError:
            self.fail('facebook_token CLI is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.env'
        self.path.write_bytes(CONTENTS)
        self.requests = []

    def boundary(self, responses, during=None):
        def call(request, timeout=20):
            self.requests.append(request)
            if during:
                during(len(self.requests))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return Response(response)
        return call

    def exchange_responses(self, returned=None):
        return [debug(), {'access_token': NEW, 'token_type': 'bearer', 'expires_in': 60 * DAY},
                returned or debug(EPOCH + 60 * DAY)]

    def test_exchange_verifies_and_changes_only_token_value_privately(self):
        with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary(self.exchange_responses())):
            result = self.module.exchange(self.path, environ={}, now=NOW)
        self.assertEqual(self.path.read_bytes(), CONTENTS.replace(OLD.encode(), NEW.encode()))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(result['state'], 'exchanged')
        self.assertEqual(result['updatedFile'], '.env')
        self.assertTrue(result['expiresAt'].endswith('+08:00'))
        self.assertEqual(len(self.requests), 3)
        for request in self.requests:
            self.assertEqual(urlsplit(request.full_url).scheme, 'https')
            self.assertEqual(urlsplit(request.full_url).hostname, 'graph.facebook.com')
        query = parse_qs(urlsplit(self.requests[1].full_url).query)
        self.assertEqual(query['grant_type'], ['fb_exchange_token'])
        self.assertEqual(query['client_id'], ['123'])
        self.assertEqual(query['client_secret'], [SECRET])
        self.assertEqual(query['fb_exchange_token'], [OLD])
        self.assertEqual(self.requests[0].get_header('Authorization'), 'Bearer 123|' + SECRET)
        encoded = json.dumps(result)
        for private in (OLD, NEW, SECRET, '999'):
            self.assertNotIn(private, encoded)

    def test_status_reports_dates_without_identity_or_secret(self):
        with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary([debug()])):
            result = self.module.status(self.path, environ={}, now=NOW)
        self.assertTrue(result['valid'])
        self.assertEqual(result['tokenType'], 'USER')
        self.assertEqual(result['expiresAt'], '2026-11-01T17:00:00+08:00')
        self.assertEqual(self.path.read_bytes(), CONTENTS)
        for private in (OLD, SECRET, '999'):
            self.assertNotIn(private, json.dumps(result))

    def test_graph_version_matches_existing_major_minor_configuration(self):
        self.path.write_bytes(CONTENTS.replace(b'v99.0', b'v99.1'))
        with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary([debug()])):
            result = self.module.status(self.path, environ={}, now=NOW)
        self.assertTrue(result['valid'])
        self.assertEqual(urlsplit(self.requests[0].full_url).path, '/v99.1/debug_token')
        self.path.write_bytes(CONTENTS.replace(b'v99.0', b'v99.1/other'))
        with mock.patch.object(self.module, 'urlopen', side_effect=AssertionError('no request expected')):
            with self.assertRaises(self.module.TokenError):
                self.module.status(self.path, environ={}, now=NOW)

    def test_remaining_fifty_days_or_more_skips_exchange(self):
        with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary([debug(EPOCH + 50 * DAY)])):
            result = self.module.exchange(self.path, environ={}, now=NOW)
        self.assertEqual(result['state'], 'already_long_lived')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.path.read_bytes(), CONTENTS)

    def test_preflight_rejects_wrong_app_type_expiry_or_missing_proof(self):
        cases = [debug(app_id='456'), debug(type='PAGE'), debug(EPOCH - 1),
                 debug(user_id=None), debug(scopes=None)]
        for body in cases:
            with self.subTest(body=body):
                self.requests.clear()
                with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary([body])):
                    with self.assertRaises(self.module.TokenError):
                        self.module.exchange(self.path, environ={}, now=NOW)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.path.read_bytes(), CONTENTS)

    def test_returned_token_must_keep_app_user_grants_and_extend_expiry(self):
        cases = [debug(EPOCH + 60 * DAY, app_id='456'), debug(EPOCH + 60 * DAY, user_id='998'),
                 debug(EPOCH + 60 * DAY, type='PAGE'), debug(EPOCH + 60 * DAY, scopes=['public_profile']),
                 debug(EPOCH + 3600)]
        for body in cases:
            with self.subTest(body=body):
                with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary(self.exchange_responses(body))):
                    with self.assertRaises(self.module.TokenError):
                        self.module.exchange(self.path, environ={}, now=NOW)
                self.assertEqual(self.path.read_bytes(), CONTENTS)

    def test_any_exported_access_token_refuses_write_before_requests(self):
        for value in (OLD, '', 'OTHER_TOKEN'):
            with self.subTest(value=value):
                with mock.patch.object(self.module, 'urlopen', side_effect=AssertionError('no request expected')):
                    with self.assertRaises(self.module.TokenError) as caught:
                        self.module.exchange(self.path, environ={'FB_ACCESS_TOKEN': value}, now=NOW)
                self.assertEqual(caught.exception.reason, 'exported_token')
                self.assertEqual(self.path.read_bytes(), CONTENTS)

    def test_env_duplicate_oversize_and_symlink_are_rejected(self):
        for contents in (CONTENTS + b'FB_ACCESS_TOKEN=duplicate\n', b'#' * 65537):
            self.path.write_bytes(contents)
            with mock.patch.object(self.module, 'urlopen', side_effect=AssertionError('no request expected')):
                with self.assertRaises(self.module.TokenError):
                    self.module.exchange(self.path, environ={}, now=NOW)
        self.path.write_bytes(CONTENTS)
        link = self.path.parent / 'linked.env'
        link.symlink_to(self.path)
        with self.assertRaises(self.module.TokenError):
            self.module.exchange(link, environ={}, now=NOW)

    def test_network_period_manual_edit_is_preserved(self):
        edited = CONTENTS.replace(OLD.encode(), b'MANUAL_TOKEN')
        def edit(index):
            if index == 3:
                self.path.write_bytes(edited)
        with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary(self.exchange_responses(), edit)):
            with self.assertRaises(self.module.TokenError) as caught:
                self.module.exchange(self.path, environ={}, now=NOW)
        self.assertEqual(caught.exception.reason, 'file_changed')
        self.assertEqual(self.path.read_bytes(), edited)

    def test_failed_atomic_replace_retains_old_bytes_and_cleans_stage(self):
        with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary(self.exchange_responses())), \
                mock.patch.object(self.module.os, 'replace', side_effect=OSError(SECRET)):
            with self.assertRaises(self.module.TokenError) as caught:
                self.module.exchange(self.path, environ={}, now=NOW)
        self.assertEqual(self.path.read_bytes(), CONTENTS)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual([path.name for path in self.path.parent.glob('.env.*') if path.name != '.env.lock'], [])

    def test_redirects_are_rejected_and_errors_never_echo_remote_values(self):
        for code in (302, 190):
            body = {'error': {'code': code, 'message': OLD + SECRET + '999'}}
            response = HTTPError('https://graph.facebook.com/?secret=' + SECRET,
                                 302 if code == 302 else 400, SECRET, {}, io.BytesIO(json.dumps(body).encode()))
            output = io.StringIO()
            with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary([response])), redirect_stdout(output):
                exit_code = self.module.main(['status', '--env-file', str(self.path)], environ={}, now=NOW)
            self.assertNotEqual(exit_code, 0)
            for private in (OLD, NEW, SECRET, '999'):
                self.assertNotIn(private, output.getvalue())

    def test_malformed_and_oversized_responses_fail_without_output_leaks(self):
        for payload in ((OLD + SECRET).encode(), b'{' + b'x' * 65536):
            with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary([payload])):
                with self.assertRaises(self.module.TokenError) as caught:
                    self.module.status(self.path, environ={}, now=NOW)
            self.assertNotIn(OLD, str(caught.exception))
            self.assertNotIn(SECRET, str(caught.exception))

    def test_http_error_body_read_failure_is_masked(self):
        class BrokenBody(io.BytesIO):
            def read(self, *args):
                raise OSError(SECRET)
        response = HTTPError('https://graph.facebook.com/?secret=' + SECRET, 400, SECRET, {}, BrokenBody())
        with mock.patch.object(self.module, 'urlopen', side_effect=self.boundary([response])):
            with self.assertRaises(self.module.TokenError) as caught:
                self.module.status(self.path, environ={}, now=NOW)
        self.assertNotIn(SECRET, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
