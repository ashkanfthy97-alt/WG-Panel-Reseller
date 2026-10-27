"""Panel-managed Telegram bots with the same bounded reseller routes as the UI.

No administrator identity, Telegram administrator registry or shell commands are
used. The panel service owns polling; credentials remain encrypted on disk.
"""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import io
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
                 'urllib3.connection', 'requests.packages.urllib3.connectionpool'):
        logger = logging.getLogger(name)
        if not any(isinstance(item, TelegramTokenFilter) for item in logger.filters):
            logger.addFilter(TelegramTokenFilter())


class BotError(ValueError):
    """A deliberately secret-free error suitable for the panel."""


class TelegramTransport:
    def call(self, token, method, payload=None, *, document=None):
        protect_transport_logs()
        if method not in {'getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage', 'sendDocument'}:
            raise BotError('unsupported_telegram_operation')
        try:
            with requests.Session() as client:
                client.trust_env = False
                result = client.post('https://api.telegram.org/bot' + token + '/' + method,
                    data=payload or {}, files=({'document': ('wireguard.conf', io.BytesIO(document),
                    'text/plain')} if document is not None else None),
                    timeout=(5, 40 if method == 'getUpdates' else 15), allow_redirects=False)
                if len(result.content) > 4 * 1024 * 1024:
                    raise BotError('telegram_response_invalid')
                value = result.json()
                if result.status_code != 200 or not isinstance(value, dict) or value.get('ok') is not True:
                    # Never forward Telegram's body or requests exceptions: they
                    # can contain a credential-bearing URL.
                    raise BotError('telegram_unavailable')
                return value.get('result')
        except (requests.RequestException, ValueError, TypeError):
            raise BotError('telegram_unavailable') from None


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

    def command(self, row, update):
        message = update.get('message') or {}
        chat, sender = message.get('chat') or {}, message.get('from') or {}
        if (chat.get('type') != 'private' or chat.get('id') != row['owner_id'] or
                sender.get('id') != row['owner_id'] or sender.get('is_bot')):
            return None
        if not self.account(row) or not self.current(row):
            return ('text', 'پنل شما غیرفعال است؛ با مدیر تماس بگیرید. / Panel access is paused.')
        text = str(message.get('text') or '').strip()
        head, _, argument = text.partition(' ')
        command = head.split('@', 1)[0].lower()
        operation = 'telegram-update-' + str(row['bot_id']) + '-' + str(update['update_id'])
        if command in {'/start', '/help'}:
            return ('text', 'ساخت کانفیگ / Create:\n/new name | days | GB\n'
                    '/peers\n/config ID\n/link ID\n/rename ID | name\n/renew ID | days | GB')
        if command == '/new':
            parts = [part.strip() for part in argument.split('|')]
            if len(parts) != 3 or not re.fullmatch(r'[0-9]{1,3}', parts[1]) or not re.fullmatch(r'[0-9]{1,6}', parts[2]):
                return ('text', '/new name | days | GB')
            code, result, _ = self.dispatch(row, 'POST', '/reseller/api/peers',
                payload={'name': parts[0], 'days': int(parts[1]), 'volume_gb': int(parts[2])}, operation=operation)
            if code in {200, 201} and result and result.get('id'):
                return ('text', 'Peer ID: ' + str(result['id']) + '\n/config ' + str(result['id']) + '\n/link ' + str(result['id']))
        elif command == '/peers':
            code, result, _ = self.dispatch(row, 'GET', '/reseller/api/peers')
            if code == 200 and isinstance(result, dict):
                lines = [str(peer['id']) + ' — ' + str(peer.get('name', '')) for peer in result.get('peers', [])]
                return ('text', '\n'.join(lines)[:3500] or 'بدون مشتری / No peers')
        elif command in {'/config', '/link'} and re.fullmatch(r'[1-9][0-9]{0,12}', argument):
            path = '/reseller/api/peers/' + argument + ('/config' if command == '/config' else '/user-link')
            code, result, body = self.dispatch(row, 'GET', path)
            if code == 200:
                if command == '/config':
                    return ('document', body)
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
                self.store.progress(row, status='running')
                updates = self.transport().call(token, 'getUpdates', {'offset': fresh['offset'],
                    'timeout': 25, 'limit': 20, 'allowed_updates': '["message"]'})
                if not isinstance(updates, list):
                    raise BotError('telegram_response_invalid')
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
                        kind, value = answer
                        if kind == 'document':
                            self.transport().call(token, 'sendDocument', {'chat_id': row['owner_id']}, document=value)
                        else:
                            self.transport().call(token, 'sendMessage', {'chat_id': row['owner_id'], 'text': value,
                                'disable_web_page_preview': 'true'})
            except Exception:
                self.store.progress(row, status='connection_error')
                stop.wait(10)

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
        workers = {}
        try:
            while True:
                rows = {row['reseller_id']: row for row in self.store.rows()}
                for account_id, (row, stop, thread) in list(workers.items()):
                    if account_id not in rows or rows[account_id]['revision'] != row['revision'] or not thread.is_alive():
                        stop.set()
                        # A replacement must never consume updates until the old
                        # long poll/request has finished.
                        thread.join(timeout=50)
                        if not thread.is_alive():
                            workers.pop(account_id)
                for account_id, row in rows.items():
                    if account_id not in workers:
                        stop = threading.Event()
                        thread = threading.Thread(target=self.poll, args=(row, stop), daemon=True,
                                                  name='reseller-bot-' + str(account_id))
                        workers[account_id] = (row, stop, thread)
                        thread.start()
                time.sleep(3)
        finally:
            for _, stop, thread in workers.values():
                stop.set()
            for _, _, thread in workers.values():
                # Retain the leader lock until every old poller has stopped.
                # A native operation may outlast an HTTP polling timeout.
                thread.join()

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
                return jsonify(error=str(error)), 400
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
