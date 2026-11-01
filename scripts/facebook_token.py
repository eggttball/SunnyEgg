#!/usr/bin/env python3
"""Inspect or exchange one explicitly configured User token; never print secrets."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

import facebook

MAX_BYTES = 65536
DAY = 86400
LONG_LIVED_MIN = 50 * DAY
LONG_LIVED_MAX = 65 * DAY
TAIPEI = ZoneInfo('Asia/Taipei')
MESSAGES = {
    'configuration': '請在指定的私密設定檔設定 FB_APP_ID、FB_APP_SECRET 與 FB_ACCESS_TOKEN。',
    'expired': 'FB_ACCESS_TOKEN 無效或已過期；請重新產生有效的 User token 並更新設定檔。',
    'wrong_app': '權杖所屬 App 與 FB_APP_ID 不符；未更新設定檔。',
    'wrong_type': '只支援有效的 User access token；未更新設定檔。',
    'invalid_response': 'Facebook 未提供完整且可驗證的權杖資料；未更新設定檔。',
    'changed_identity': '新權杖的使用者或權限與原權杖不一致；未更新設定檔。',
    'not_extended': '新權杖未提供約 60 天且更長的有效期限；未更新設定檔。',
    'network': 'Facebook 權杖服務連線失敗；請稍後重試。',
    'remote': 'Facebook 拒絕權杖作業；請確認 App 設定與權杖後重試。',
    'redirect': 'Facebook 權杖服務回傳重新導向；已停止連線且未更新設定檔。',
    'exported_token': '請先 unset FB_ACCESS_TOKEN（包含空的 export），再執行 exchange，避免環境變數覆蓋新權杖。',
    'file_changed': '設定檔在作業期間已變更；已保留最新檔案，請重新執行。',
    'locked': '已有權杖作業執行中；請稍後重試。',
    'write_failed': '私密設定檔尚未確認更新；請檢查檔案權限後重試。',
    'arguments': '請使用 status 或 exchange，並以 --env-file 指定私密設定檔。',
    'cancelled': '權杖作業已取消。',
}


class TokenError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in MESSAGES else 'remote'
        super().__init__(MESSAGES[self.reason])


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise TokenError('redirect')


# Fixed Graph endpoints and a replaceable network boundary for synthetic tests.
urlopen = build_opener(NoRedirectHandler()).open


def _request(endpoint, fields, version, bearer=None):
    if endpoint not in {'debug_token', 'oauth/access_token'}:
        raise TokenError('configuration')
    prefix = '/' + version if version else ''
    url = 'https://graph.facebook.com' + prefix + '/' + endpoint + '?' + urlencode(fields)
    headers = {'Accept': 'application/json'}
    if bearer is not None:
        headers['Authorization'] = 'Bearer ' + bearer
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        code = error.code
        try:
            try:
                payload = error.read(MAX_BYTES + 1)
            except (OSError, ValueError):
                payload = b''
        finally:
            error.close()
        if 300 <= code < 400:
            raise TokenError('redirect') from None
        try:
            body = json.loads(payload.decode('utf-8')) if len(payload) <= MAX_BYTES else {}
        except (ValueError, UnicodeError):
            body = {}
        if isinstance(body, dict) and isinstance(body.get('error'), dict) and body['error'].get('code') in {102, 190}:
            raise TokenError('expired') from None
        raise TokenError('remote') from None
    except (URLError, OSError, ValueError) as error:
        if isinstance(error, TokenError):
            raise
        raise TokenError('network') from None
    if len(payload) > MAX_BYTES:
        raise TokenError('invalid_response')
    try:
        body = json.loads(payload.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise TokenError('invalid_response') from None
    if not isinstance(body, dict):
        raise TokenError('invalid_response')
    if 'error' in body:
        error = body['error']
        raise TokenError('expired' if isinstance(error, dict) and error.get('code') in {102, 190} else 'remote')
    return body


def _snapshot(filename):
    try:
        path = Path(filename)
        if path.is_symlink() or any(ord(c) < 32 or ord(c) == 127 for c in os.fspath(path)):
            raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError()
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError()
        values = facebook.parse_environment_content(raw)
        return {'path': path, 'raw': raw, 'identity': (info.st_dev, info.st_ino), 'values': values}
    except (OSError, ValueError, TypeError):
        raise TokenError('configuration') from None


def _opaque(value):
    return (isinstance(value, str) and 0 < len(value) <= 16384
            and all(33 <= ord(c) <= 126 and c not in "'\"\\" for c in value))


def _settings(snapshot, environ):
    values = snapshot['values'].copy()
    for key in facebook.ENV_KEYS:
        if key in environ:
            values[key] = environ[key]
    app_id, secret, token = (values.get(key) for key in ('FB_APP_ID', 'FB_APP_SECRET', 'FB_ACCESS_TOKEN'))
    version = values.get('FB_GRAPH_VERSION', '')
    if (not isinstance(app_id, str) or not re.fullmatch(r'[0-9]+', app_id)
            or not _opaque(secret) or '|' in secret or not _opaque(token)
            or not isinstance(version, str) or version and not re.fullmatch(r'v[0-9]+\.[0-9]+', version)):
        raise TokenError('configuration')
    return {'app_id': app_id, 'secret': secret, 'token': token, 'version': version}


def _clock(now):
    value = datetime.now(timezone.utc) if now is None else now
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TokenError('configuration')
    return int(value.timestamp())


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 253402272000:
        raise TokenError('invalid_response')
    return value


def _debug(settings, token):
    body = _request('debug_token', {'input_token': token}, settings['version'], settings['app_id'] + '|' + settings['secret'])
    data = body.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('is_valid'), bool):
        raise TokenError('invalid_response')
    token_type = data.get('type')
    if token_type is not None and token_type not in {'USER', 'PAGE', 'APP', 'SYSTEM_USER'}:
        raise TokenError('invalid_response')
    if data['is_valid'] and data.get('app_id') != settings['app_id']:
        raise TokenError('wrong_app')
    return {'valid': data['is_valid'], 'type': token_type, 'user_id': data.get('user_id'),
            'scopes': data.get('scopes'), 'expires': _epoch(data.get('expires_at', 0)),
            'data_expires': _epoch(data.get('data_access_expires_at', 0))}


def _user(info, now):
    if not info['valid'] or info['expires'] and info['expires'] <= now:
        raise TokenError('expired')
    if info['type'] != 'USER':
        raise TokenError('wrong_type')
    if (not info['expires'] or not isinstance(info['user_id'], str) or not re.fullmatch(r'[0-9]+', info['user_id'])
            or not isinstance(info['scopes'], list) or any(not isinstance(scope, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', scope)
                                                         for scope in info['scopes'])):
        raise TokenError('invalid_response')


def _date(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(TAIPEI).isoformat() if epoch else None


def _public(info, now):
    valid = info['valid'] and (not info['expires'] or info['expires'] > now)
    result = {'state': 'valid' if valid else 'expired', 'valid': valid, 'tokenType': info['type'],
              'expiresAt': _date(info['expires']), 'dataAccessExpiresAt': _date(info['data_expires'])}
    if not valid:
        result['message'] = MESSAGES['expired']
    return result


@contextmanager
def _lock(path):
    descriptor = None
    try:
        lock_path = path.with_name(path.name + '.lock')
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TokenError('configuration')
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TokenError('locked') from None
        yield
    except OSError:
        raise TokenError('write_failed') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _persist(snapshot, token):
    lines, replaced = [], 0
    for line in snapshot['raw'].splitlines(keepends=True):
        ending = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b'\r' if line.endswith(b'\r') else b''
        body = line[:-len(ending)] if ending else line
        match = re.match(rb' *FB_ACCESS_TOKEN *= *', body)
        if match:
            old = body[match.end():]
            trailing = old[len(old.rstrip(b' ')):]
            quote = old[:1] if old[:1] in (b"'", b'"') else b''
            line = body[:match.end()] + quote + token.encode('ascii') + quote + trailing + ending
            replaced += 1
        lines.append(line)
    if replaced != 1:
        raise TokenError('configuration')
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=snapshot['path'].name + '.', suffix='.tmp', dir=snapshot['path'].parent)
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(b''.join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            current = _snapshot(snapshot['path'])
        except TokenError:
            raise TokenError('file_changed') from None
        if current['raw'] != snapshot['raw'] or current['identity'] != snapshot['identity']:
            raise TokenError('file_changed')
        os.replace(temporary, snapshot['path'])
    except OSError:
        raise TokenError('write_failed') from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def status(env_file, environ=None, now=None):
    environ = os.environ if environ is None else environ
    clock = _clock(now)
    settings = _settings(_snapshot(env_file), environ)
    return _public(_debug(settings, settings['token']), clock)


def exchange(env_file, environ=None, now=None):
    environ = os.environ if environ is None else environ
    if 'FB_ACCESS_TOKEN' in environ:
        raise TokenError('exported_token')
    clock = _clock(now)
    with _lock(Path(env_file)):
        snapshot = _snapshot(env_file)
        settings = _settings(snapshot, environ)
        original = _debug(settings, settings['token'])
        _user(original, clock)
        if original['expires'] - clock >= LONG_LIVED_MIN:
            return {**_public(original, clock), 'state': 'already_long_lived'}
        # Meta documents this endpoint as GET. Secret query values stay internal;
        # redirects and all raw HTTP/Graph errors are rejected or masked.
        body = _request('oauth/access_token', {'grant_type': 'fb_exchange_token', 'client_id': settings['app_id'],
                                             'client_secret': settings['secret'], 'fb_exchange_token': settings['token']}, settings['version'])
        token = body.get('access_token')
        duration = _epoch(body.get('expires_in'))
        if not _opaque(token) or not LONG_LIVED_MIN <= duration <= LONG_LIVED_MAX or body.get('token_type', 'bearer') != 'bearer':
            raise TokenError('invalid_response')
        returned = _debug(settings, token)
        _user(returned, clock)
        if returned['user_id'] != original['user_id'] or not set(original['scopes']).issubset(returned['scopes']):
            raise TokenError('changed_identity')
        remaining = returned['expires'] - clock
        if not LONG_LIVED_MIN <= remaining <= LONG_LIVED_MAX or returned['expires'] <= original['expires'] or abs(remaining - duration) > 300:
            raise TokenError('not_extended')
        _persist(snapshot, token)
        return {**_public(returned, clock), 'state': 'exchanged', 'updatedFile': snapshot['path'].name}


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise TokenError('arguments')


def main(argv=None, environ=None, now=None):
    try:
        parser = SafeArgumentParser(description='檢查或換取約 60 天的 Facebook User token；不輸出私密值。')
        parser.add_argument('command', choices=('status', 'exchange'))
        parser.add_argument('--env-file', required=True, type=Path)
        args = parser.parse_args(argv)
        result = (status if args.command == 'status' else exchange)(args.env_file, environ=environ, now=now)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result.get('valid') else 2
    except TokenError as error:
        print(json.dumps({'state': 'error', 'reason': error.reason, 'message': str(error)}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({'state': 'cancelled', 'message': MESSAGES['cancelled']}, ensure_ascii=False))
        return 130
    except Exception:
        print(json.dumps({'state': 'error', 'message': MESSAGES['remote']}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
