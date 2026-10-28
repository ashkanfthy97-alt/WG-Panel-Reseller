"""Panel-managed Telegram bots with the same bounded reseller routes as the UI.

No administrator identity, Telegram administrator registry or shell commands are
used. The panel service owns polling; credentials remain encrypted on disk.
"""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request, session
import requests
from werkzeug.test import Client
from werkzeug.wrappers import Response

from deploy.linux.reseller_store import ResellerStore


class TelegramTokenFilter(logging.Filter):
    def filter(self, record):
        rendered = record.getMessage()
        record.msg = re.sub(r'/bot[0-9]+:[A-Za-z0-9_-]+(?=/|\b)',
                            '/bot[REDACTED]', rendered)
        record.args = ()
        return True


def protect_transport_logs():
    # Filters on origin loggers run before propagation to native debug handlers.
    # Do not change global logger levels while several bots are making requests.
    for name in ('urllib3.connectionpool', 'urllib3.util.retry', 'urllib3.poolmanager',
                 'urllib3.connection', 'requests.packages.urllib3.connectionpool',
                 'httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2'):
        logger = logging.getLogger(name)
        if not any(isinstance(item, TelegramTokenFilter) for item in logger.filters):
            logger.addFilter(TelegramTokenFilter())


class BotError(ValueError):
    """A deliberately secret-free error suitable for the panel."""
    def __init__(self, code, *, retry_after=0):
        super().__init__(code)
        self.code = code
        self.retry_after = min(3600, max(0, int(retry_after or 0)))


def telegram_result(status, value):
    if not isinstance(value, dict):
        raise BotError('telegram_response_invalid')
    if status == 200 and value.get('ok') is True:
        return value.get('result')
    code = value.get('error_code', status)
    if code in {401, 404}:
        raise BotError('telegram_invalid_token')
    if code == 409:
        raise BotError('telegram_conflict')
    if code == 429:
        parameters = value.get('parameters') or {}
        raise BotError('telegram_rate_limited', retry_after=parameters.get('retry_after', 30))
    raise BotError('telegram_unavailable')


TELEGRAM_METHODS = {'getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage',
                    'sendDocument', 'sendPhoto', 'answerCallbackQuery'}


def telegram_payload(payload):
    return {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
            for key, value in (payload or {}).items()}


class TelegramTransport:
    def call(self, token, method, payload=None, *, document=None, photo=None):
        protect_transport_logs()
        if method not in TELEGRAM_METHODS:
            raise BotError('unsupported_telegram_operation')
        try:
            with requests.Session() as client:
                client.trust_env = False
                files = ({'document': ('wireguard.conf', io.BytesIO(document), 'text/plain')}
                         if document is not None else {'photo': ('wireguard.png', io.BytesIO(photo), 'image/png')}
                         if photo is not None else None)
                result = client.post('https://api.telegram.org/bot' + token + '/' + method,
                    data=telegram_payload(payload), files=files,
                    timeout=(5, 40 if method == 'getUpdates' else 15), allow_redirects=False)
                if len(result.content) > 4 * 1024 * 1024:
                    raise BotError('telegram_response_invalid')
                return telegram_result(result.status_code, result.json())
        except BotError:
            raise
        except requests.RequestException:
            raise BotError('telegram_connection_failed') from None
        except (ValueError, TypeError):
            raise BotError('telegram_response_invalid') from None


class BotStore:
    def __init__(self, path, secret, *, fernet_key=None):
        self.path = Path(path)
        self.secret = secret
        self.fernet_key = fernet_key

    def cipher(self):
        from cryptography.fernet import Fernet
        if self.fernet_key:
            key = self.fernet_key
        else:
            if not self.secret:
                raise BotError('panel_secret_missing')
            value = self.secret.encode() if isinstance(self.secret, str) else self.secret
            key = base64.urlsafe_b64encode(hmac.new(value,
                b'wg-portal-reseller-bots-v1', hashlib.sha256).digest())
        return Fernet(key.encode() if isinstance(key, str) else key)

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('''CREATE TABLE IF NOT EXISTS bots (
                reseller_id INTEGER PRIMARY KEY, bot_id INTEGER NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE, token_enc TEXT NOT NULL,
                owner_id INTEGER NOT NULL, username TEXT NOT NULL,
                base_url TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'starting', offset INTEGER NOT NULL DEFAULT 0,
                revision TEXT NOT NULL)''')
            if os.name != 'nt':
                os.chmod(self.path, 0o600)
            yield db
            db.commit()
        finally:
            db.close()

    def rows(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM bots WHERE enabled=1')]

    def get(self, account_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM bots WHERE reseller_id=?', (account_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def public(row):
        if not row:
            return {'configured': False, 'enabled': False, 'status': 'not_configured'}
        return {key: row[key] for key in ('enabled', 'username', 'owner_id', 'status')} | {
            'configured': True, 'enabled': bool(row['enabled'])}

    def save(self, account_id, owner_id, token, identity, base_url):
        encrypted = self.cipher().encrypt(token.encode()).decode()
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        try:
            with self.db() as db:
                previous = db.execute('SELECT * FROM bots WHERE reseller_id=?', (account_id,)).fetchone()
                offset = previous['offset'] if previous and previous['bot_id'] == identity['id'] else 0
                db.execute('''INSERT INTO bots
                    (reseller_id,bot_id,fingerprint,token_enc,owner_id,username,base_url,offset,revision)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(reseller_id) DO UPDATE SET
                    bot_id=excluded.bot_id,fingerprint=excluded.fingerprint,token_enc=excluded.token_enc,
                    owner_id=excluded.owner_id,username=excluded.username,base_url=excluded.base_url,
                    enabled=1,status='starting',offset=excluded.offset,revision=excluded.revision''',
                    (account_id, identity['id'], fingerprint, encrypted, owner_id, identity['username'],
                     base_url, offset, secrets.token_hex(16)))
        except sqlite3.IntegrityError:
            raise BotError('bot_already_assigned') from None

    def disable(self, account_id):
        # Forget the credential immediately; preserve the bot ID/offset so a
        # later reconnect does not replay already consumed create commands.
        with self.db() as db:
            db.execute("UPDATE bots SET enabled=0,status='disabled',token_enc='',revision=? WHERE reseller_id=?",
                       (secrets.token_hex(16), account_id))

    def progress(self, row, *, offset=None, status=None):
        with self.db() as db:
            if offset is not None:
                db.execute('UPDATE bots SET offset=MAX(offset,?) WHERE reseller_id=? AND revision=?',
                           (offset, row['reseller_id'], row['revision']))
            if status is not None:
                db.execute('UPDATE bots SET status=? WHERE reseller_id=? AND revision=?',
                           (status, row['reseller_id'], row['revision']))


class BotManager:
    def __init__(self, app, database):
        self.app = app
        self.accounts = ResellerStore(database)
        self.store = BotStore(Path(database).with_name('reseller_bots.sqlite3'), app.secret_key,
                              fernet_key=os.environ.get('FERNET_KEY'))
        self.started = False
        self.start_lock = threading.Lock()
        self.prompts = {}
        self.prompt_lock = threading.Lock()

    def transport(self):
        return self.app.config.get('RESELLER_BOT_TRANSPORT') or TelegramTransport()

    def current(self, row):
        fresh = self.store.get(row['reseller_id'])
        return fresh if fresh and fresh['enabled'] and fresh['revision'] == row['revision'] else None

    def account(self, row):
        value = self.accounts.get(row['reseller_id'])
        return value if value and value.get('active') else None

    def dispatch(self, row, method, path, *, payload=None, operation=None):
        account = self.account(row)
        if not account or not self.current(row):
            return 403, {'error': 'panel_paused'}, b''
        # Fresh WSGI request goes through the real reseller permission, quota,
        # ownership and idempotency checks. It never authenticates as an admin.
        from portal_reseller_native import ResellerSessions
        with self.app.app_context():
            serializer = self.app.session_interface.get_signing_serializer(self.app)
            cookie = serializer.dumps({'reseller_identity': {'id': account['id'],
                'version': account['auth_version']}})
            client = Client(self.app, Response, use_cookies=True)
            client.set_cookie(ResellerSessions.cookie_name, cookie,
                              domain=urlsplit(row['base_url']).hostname)
            initial = client.get('/reseller/api/session', base_url=row['base_url'])
            try:
                state = initial.get_json(silent=True) or {}
            finally:
                initial.close()
            if not state.get('csrf') or not state.get('native_csrf'):
                return 503, {'error': 'csrf_unavailable'}, b''
            headers = {'Origin': row['base_url'].rstrip('/'), 'X-Reseller-CSRF': state['csrf'],
                       'X-CSRFToken': state['native_csrf']}
            if operation:
                headers['Idempotency-Key'] = operation
            response = client.open(path, method=method,
                json=payload, headers=headers, base_url=row['base_url'])
            try:
                return response.status_code, response.get_json(silent=True), response.get_data()
            finally:
                response.close()

    @staticmethod
    def private_message(row, update):
        callback = update.get('callback_query') or {}
        message = callback.get('message') or update.get('message') or {}
        sender = callback.get('from') or message.get('from') or {}
        chat = message.get('chat') or {}
        return (message if chat.get('type') == 'private' and chat.get('id') == row['owner_id']
                and sender.get('id') == row['owner_id'] and not sender.get('is_bot') else None)

    def callback_id(self, row, update):
        callback = update.get('callback_query') or {}
        value = callback.get('id')
        return value if self.private_message(row, update) and isinstance(value, str) and len(value) <= 256 else None

    @staticmethod
    def peer_keyboard(peer_id):
        return {'inline_keyboard': [[
            {'text': 'کانفیگ / Config', 'callback_data': 'config:' + str(peer_id)},
            {'text': 'QR', 'callback_data': 'qr:' + str(peer_id)}], [
            {'text': 'لینک کاربر / User link', 'callback_data': 'link:' + str(peer_id)},
            {'text': 'نام / تمدید', 'callback_data': 'manage:' + str(peer_id)}]]}

    def peer_exists(self, row, peer_id):
        code, value, _ = self.dispatch(row, 'GET', '/reseller/api/peers')
        return code == 200 and isinstance(value, dict) and any(
            peer.get('id') == peer_id for peer in value.get('peers', []))

    def command(self, row, update):
        message = self.private_message(row, update)
        if message is None:
            return None
        if not self.account(row) or not self.current(row):
            return ('text', 'پنل شما غیرفعال است؛ با مدیر تماس بگیرید. / Panel access is paused.')
        callback = update.get('callback_query') or {}
        text = str(message.get('text') or '').strip()
        prompt_key = (row['reseller_id'], row['revision'])
        with self.prompt_lock:
            now = time.monotonic()
            for key, prompt in list(self.prompts.items()):
                if prompt[2] <= now or (key[0] == row['reseller_id'] and key != prompt_key):
                    self.prompts.pop(key, None)
            if callback or text.startswith('/'):
                self.prompts.pop(prompt_key, None)
        if callback:
            value = callback.get('data') or ''
            if value == 'credit':
                text = '/credit'
            elif re.fullmatch(r'page:[0-9]{1,6}', value):
                text = '/peers ' + value.split(':')[1]
            else:
                matched = re.fullmatch(r'(config|qr|link|manage|rename|renew):([1-9][0-9]{0,12})', value)
                if not matched:
                    return ('text', '/help')
                action, peer_id = matched[1], int(matched[2])
                if not self.peer_exists(row, peer_id):
                    return ('text', 'مشتری در دسترس نیست. / Peer unavailable.')
                if action == 'manage':
                    return ('messages', [{'text': 'نام یا تمدید / Name or renewal', 'reply_markup': {
                        'inline_keyboard': [[{'text': 'تغییر نام / Rename', 'callback_data': 'rename:' + str(peer_id)},
                                             {'text': 'تمدید / Renew', 'callback_data': 'renew:' + str(peer_id)}]]}}])
                if action in {'rename', 'renew'}:
                    with self.prompt_lock:
                        self.prompts[prompt_key] = (action, peer_id, time.monotonic() + 300)
                    return ('text', 'نام جدید را بفرستید؛ لغو: /cancel' if action == 'rename' else
                            'روز و حجم را بفرستید: days | GB — لغو: /cancel')
                text = '/' + action + ' ' + str(peer_id)
        elif text == '/cancel':
            with self.prompt_lock:
                self.prompts.pop(prompt_key, None)
            return ('text', 'لغو شد / Cancelled')
        elif not text.startswith('/'):
            with self.prompt_lock:
                prompt = self.prompts.pop(prompt_key, None)
            if prompt and prompt[2] > time.monotonic():
                text = '/' + prompt[0] + ' ' + str(prompt[1]) + ' | ' + text
            else:
                text = '/help'
        head, _, argument = text.partition(' ')
        command = head.split('@', 1)[0].lower()
        operation = 'telegram-update-' + str(row['bot_id']) + '-' + str(update['update_id'])
        if command in {'/start', '/help'}:
            return ('messages', [{'text': 'ساخت کانفیگ / Create:\n/new name | days | GB\n'
                    '/peers\n/config ID\n/qr ID\n/link ID\n/rename ID | name\n/renew ID | days | GB',
                'reply_markup': {'inline_keyboard': [[{'text': 'کانفیگ‌های من / My configs', 'callback_data': 'page:0'},
                                                     {'text': 'اعتبار / Credit', 'callback_data': 'credit'}]]}}])
        if command == '/credit':
            code, result, _ = self.dispatch(row, 'GET', '/reseller/api/credit')
            if code == 200 and isinstance(result, dict):
                value = result.get('credit') or {}
                seconds = int(value.get('remaining_seconds', 0))
                return ('text', 'اعتبار: ' + str(seconds // 86400) + ' روز، ' + str(seconds % 86400 // 3600) +
                        ' ساعت\nPeer باقی‌مانده: ' + str(value.get('remaining_peers', 0)) +
                        ' / ' + str(value.get('max_peers', 0)))
        elif command == '/new':
            parts = [part.strip() for part in argument.split('|')]
            if len(parts) != 3 or not re.fullmatch(r'[0-9]{1,3}', parts[1]) or not re.fullmatch(r'[0-9]{1,6}', parts[2]):
                return ('text', '/new name | days | GB')
            code, result, _ = self.dispatch(row, 'POST', '/reseller/api/peers',
                payload={'name': parts[0], 'days': int(parts[1]), 'volume_gb': int(parts[2])}, operation=operation)
            if code in {200, 201} and result and result.get('id'):
                return ('messages', [{'text': 'Peer ID: ' + str(result['id']),
                    'reply_markup': self.peer_keyboard(result['id'])}])
        elif command == '/peers':
            code, result, _ = self.dispatch(row, 'GET', '/reseller/api/peers')
            if code == 200 and isinstance(result, dict):
                peers = result.get('peers', [])
                page = int(argument) if re.fullmatch(r'[0-9]{1,6}', argument) else 0
                start = page * 10
                messages = [{'text': str(peer['id']) + ' — ' + str(peer.get('name', ''))[:256],
                             'reply_markup': self.peer_keyboard(peer['id'])} for peer in peers[start:start + 10]]
                if start + 10 < len(peers):
                    messages.append({'text': 'صفحه بعد / Next page', 'reply_markup': {'inline_keyboard': [[
                        {'text': 'بعدی / Next', 'callback_data': 'page:' + str(page + 1)}]]}})
                return ('messages', messages or [{'text': 'بدون مشتری / No peers'}])
        elif command in {'/config', '/link', '/qr'} and re.fullmatch(r'[1-9][0-9]{0,12}', argument):
            path = '/reseller/api/peers/' + argument + {'/config': '/config', '/link': '/user-link', '/qr': '/qr'}[command]
            code, result, body = self.dispatch(row, 'GET', path)
            if code == 200:
                if command in {'/config', '/qr'}:
                    return ('document' if command == '/config' else 'photo', body)
                if isinstance(result, dict) and isinstance(result.get('url'), str):
                    return ('text', result['url'])
        elif command in {'/rename', '/renew'}:
            parts = [part.strip() for part in argument.split('|')]
            if not parts or not re.fullmatch(r'[1-9][0-9]{0,12}', parts[0]):
                return ('text', '/help')
            if command == '/rename' and len(parts) == 2:
                payload, action = {'name': parts[1]}, 'edit'
            elif command == '/renew' and len(parts) == 3 and all(re.fullmatch(r'[0-9]{1,6}', p) for p in parts[1:]):
                payload, action = {'days': int(parts[1]), 'volume_gb': int(parts[2])}, 'renew'
            else:
                return ('text', '/help')
            code, result, _ = self.dispatch(row, 'POST', '/reseller/api/peers/' + parts[0] + '/' + action,
                                           payload=payload, operation=operation)
            if code == 200:
                return ('text', 'انجام شد / Done')
        else:
            return ('text', '/help')
        return ('text', 'عملیات انجام نشد؛ مشخصات، اعتبار و وضعیت درخواست را در پنل بررسی کنید. / '
                'Not completed. Check the request and your panel access before retrying.')

    @staticmethod
    def error_status(error):
        value = getattr(error, 'code', '')
        return value if value in {'telegram_invalid_token', 'telegram_conflict', 'telegram_rate_limited',
                                 'telegram_connection_failed', 'telegram_unavailable'} else 'connection_error'

    def send_answer(self, row, token, answer):
        kind, value = answer
        if kind == 'document':
            self.transport().call(token, 'sendDocument', {'chat_id': row['owner_id']}, document=value)
        elif kind == 'photo':
            self.transport().call(token, 'sendPhoto', {'chat_id': row['owner_id']}, photo=value)
        else:
            for item in value if kind == 'messages' else [{'text': value}]:
                if not self.current(row) or not self.account(row):
                    break
                self.transport().call(token, 'sendMessage', {'chat_id': row['owner_id'],
                    'disable_web_page_preview': 'true', **item})

    def poll(self, row, stop):
        try:
            token = self.store.cipher().decrypt(row['token_enc'].encode()).decode()
        except Exception:
            self.store.progress(row, status='credential_error')
            return
        while not stop.is_set() and self.current(row):
            try:
                fresh = self.current(row)
                if not fresh:
                    return
                if not self.account(row):
                    self.store.progress(row, status='panel_paused')
                    stop.wait(5)
                    continue
                updates = self.transport().call(token, 'getUpdates', {'offset': fresh['offset'],
                    'timeout': 25, 'limit': 20, 'allowed_updates': '["message","callback_query"]'})
                if not isinstance(updates, list):
                    raise BotError('telegram_response_invalid')
                self.store.progress(row, status='running')
                for update in updates:
                    if stop.is_set() or not self.current(row):
                        return
                    if not isinstance(update, dict) or type(update.get('update_id')) is not int:
                        continue
                    if update['update_id'] < fresh['offset']:
                        continue
                    answer = self.command(row, update)
                    # Create/renew already have durable idempotency at this point.
                    # Commit the offset before Telegram delivery, which must not
                    # cause the same paid operation to execute again on failure.
                    self.store.progress(row, offset=update['update_id'] + 1)
                    if answer and self.current(row) and self.account(row):
                        callback_id = self.callback_id(row, update)
                        if callback_id:
                            self.transport().call(token, 'answerCallbackQuery', {'callback_query_id': callback_id})
                        self.send_answer(row, token, answer)
            except Exception as error:
                self.store.progress(row, status=self.error_status(error))
                stop.wait(max(10, getattr(error, 'retry_after', 0)))

    def supervise(self):
        import fcntl
        lock_path = self.store.path.with_suffix('.polling.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        while True:
            try:
                with open(lock_path, 'a+b') as lock:
                    os.chmod(lock_path, 0o600)
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(10)
                        continue
                    self.run_leader()
            except Exception:
                time.sleep(10)

    def run_leader(self):
        import asyncio
        from portal_bot_runtime import AsyncBotRuntime
        asyncio.run(AsyncBotRuntime(self).run())

    def ensure_started(self):
        if self.app.testing or not sys.platform.startswith('linux') or self.app.config.get('RESELLER_BOTS_DISABLED'):
            return
        with self.start_lock:
            if not self.started:
                self.started = True
                threading.Thread(target=self.supervise, daemon=True, name='reseller-bots-supervisor').start()


def install_reseller_bot(app, csrf, database):
    if 'reseller_bot' in app.blueprints:
        return
    manager = BotManager(app, database)
    app.extensions['reseller_bots'] = manager
    bp = Blueprint('reseller_bot', __name__)

    @bp.errorhandler(400)
    def security_failure(_error):
        # Flask-WTF may reject before the view. Keep its protection and return
        # a usable error without reflecting token-bearing input or internals.
        return jsonify(error='bot_csrf_rejected',
                       message='نشست امنیتی معتبر نیست؛ صفحه را تازه کنید و دوباره تلاش کنید.'), 400

    @bp.route('/reseller/api/bot', methods=['GET', 'POST', 'DELETE'])
    def settings():
        request.max_content_length = 4096
        identity = session.get('reseller_identity') or {}
        account = manager.accounts.get(identity.get('id', 0)) if isinstance(identity, dict) else None
        if not account or account['auth_version'] != identity.get('version'):
            return jsonify(error='login_required'), 401
        if not account['active']:
            return jsonify(error='panel_paused'), 403
        if request.method != 'GET':
            expected = session.get('reseller_csrf') or ''
            supplied = request.headers.get('X-Reseller-CSRF') or ''
            origin = urlsplit(request.headers.get('Origin') or '')
            if (not expected or not supplied or not hmac.compare_digest(expected, supplied) or
                    origin.scheme != request.scheme or origin.netloc.lower() != request.host.lower()):
                return jsonify(error='invalid_csrf'), 403
        if request.method == 'DELETE':
            manager.store.disable(account['id'])
        elif request.method == 'POST':
            if request.content_length and request.content_length > 4096:
                return jsonify(error='invalid_request'), 413
            value = request.get_json(silent=True)
            if not isinstance(value, dict) or set(value) != {'owner_id', 'token'}:
                return jsonify(error='invalid_bot_settings'), 400
            raw_owner = value['owner_id']
            if isinstance(raw_owner, bool) or not re.fullmatch(r'[1-9][0-9]{0,15}', str(raw_owner)):
                return jsonify(error='invalid_owner_id'), 400
            token = value['token']
            if not isinstance(token, str) or not re.fullmatch(r'[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,120}', token):
                return jsonify(error='invalid_bot_token'), 400
            native_module = sys.modules.get(app.import_name)
            native_loader = getattr(native_module, '_load_tg_settings', None)
            try:
                native = native_loader() if native_loader else {}
                native_tokens = [native.get('bot_token', ''), os.environ.get('TG_BOT_TOKEN', ''),
                                 os.environ.get('TELEGRAM_BOT_TOKEN', '')]
                if any(item and token.split(':')[0] == str(item).split(':')[0] for item in native_tokens):
                    return jsonify(error='native_bot_conflict'), 409
                info = manager.transport().call(token, 'getMe')
                if (not isinstance(info, dict) or type(info.get('id')) is not int or not info.get('is_bot') or
                        not re.fullmatch(r'[A-Za-z0-9_]{5,64}', str(info.get('username') or '')) or
                        str(info['id']) != token.split(':')[0]):
                    raise BotError('invalid_bot_token')
                webhook = manager.transport().call(token, 'getWebhookInfo')
                if not isinstance(webhook, dict) or webhook.get('url'):
                    return jsonify(error='bot_webhook_in_use'), 409
                base_url = app.config.get('RESELLER_BOT_PUBLIC_URL') or request.host_url
                parsed = urlsplit(base_url)
                if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or
                        parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
                    raise BotError('invalid_panel_url')
                manager.store.save(account['id'], int(raw_owner), token, info, base_url.rstrip('/'))
            except BotError as error:
                code = 429 if error.code == 'telegram_rate_limited' else 409 if error.code in {
                    'telegram_conflict', 'bot_already_assigned'} else 503 if error.code in {
                    'telegram_connection_failed', 'telegram_unavailable'} else 400
                return jsonify(error=error.code, retry_after=error.retry_after), code
            except Exception:
                return jsonify(error='bot_configuration_failed'), 503
        manager.ensure_started()
        factory = app.config.get('RESELLER_BOT_CSRF_FACTORY') if app.testing else None
        if not callable(factory):
            from flask_wtf.csrf import generate_csrf
            factory = generate_csrf
        return jsonify(bot=manager.store.public(manager.store.get(account['id'])), native_csrf=factory())

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.after_request
    def start_bot_supervisor(response):
        manager.ensure_started()
        return response

    app.register_blueprint(bp)
