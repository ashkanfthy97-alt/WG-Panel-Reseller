"""Reseller tab inside WG_Panel's existing Flask application and service.

Native operations run through their existing authenticated WSGI routes in the
same process. No loopback HTTP server, gateway, or extra listening port is used.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

from flask import request
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import BadSignature
from werkzeug.http import parse_cookie
from werkzeug.test import Client
from werkzeug.wrappers import Response

from deploy.linux.portal_reseller import CoreError, reseller_blueprint


def _reseller_path(path):
    return path in {'/reseller', '/resellers'} or path.startswith(('/reseller/', '/resellers/'))


class ResellerSessions(SecureCookieSessionInterface):
    """Leave native sessions unchanged and sign reseller sessions separately."""
    salt = 'wg-panel-reseller-session-v1'
    cookie_name = 'wgpanel_reseller_session'
    lifetime = timedelta(minutes=30)

    def __init__(self, native):
        self.native = native

    def open_session(self, app, req):
        if not _reseller_path(req.path):
            return self.native.open_session(app, req)
        serializer = self.get_signing_serializer(app)
        if serializer is None:
            return None
        value = req.cookies.get(self.cookie_name)
        if value:
            try:
                return self.session_class(serializer.loads(value, max_age=int(self.lifetime.total_seconds())))
            except BadSignature:
                pass
        return self.session_class()

    def save_session(self, app, session, response):
        if not _reseller_path(request.path):
            return self.native.save_session(app, session, response)
        return super().save_session(app, session, response)

    def get_cookie_name(self, app):
        return self.cookie_name if _reseller_path(request.path) else self.native.get_cookie_name(app)

    def get_cookie_domain(self, app):
        return None

    def get_cookie_path(self, app):
        return '/'

    def get_cookie_secure(self, app):
        return request.is_secure

    def get_cookie_httponly(self, app):
        return True

    def get_cookie_samesite(self, app):
        return 'Strict'

    def get_expiration_time(self, app, session):
        return datetime.now(timezone.utc) + self.lifetime if session.permanent else None


class ResellerCookies:
    """Keep native login/remember cookies out of reseller request contexts.

    The original native cookies are available only to the server-side owner
    check, whose separate native request verifies them with native login code.
    """
    def __init__(self, wsgi, session_name, remember_name):
        self.wsgi = wsgi
        self.owner_names = (session_name, remember_name)

    def __call__(self, environ, start_response):
        if _reseller_path(environ.get('PATH_INFO', '')):
            environ = environ.copy()
            cookies = parse_cookie(environ.get('HTTP_COOKIE', ''))
            environ['wgpanel.reseller_owner_cookie'] = '; '.join(
                name + '=' + cookies[name] for name in self.owner_names if cookies.get(name))
            value = cookies.get(ResellerSessions.cookie_name)
            environ['HTTP_COOKIE'] = ResellerSessions.cookie_name + '=' + value if value else ''
        return self.wsgi(environ, start_response)


class NativeApplicationCore:
    """Bounded internal dispatch with fresh Flask and login state per call."""
    def __init__(self, app):
        self.app = app

    def call(self, method, path, payload=None, *, cookie=None):
        allowed = (
            method == 'GET' and path in {'/api/admin', '/api/get-interfaces', '/api/peers'}
            or method == 'GET' and re.fullmatch(r'/api/iface/[1-9][0-9]*/peer-defaults', path)
            or method == 'GET' and re.fullmatch(r'/api/peer/[A-Za-z0-9%_+\-=]+/(?:config_qr|config\?download=1)', path)
            or method == 'POST' and path in {'/api/peers', '/api/portal/owned-peer-action'}
        )
        if not allowed or (cookie is not None and path != '/api/admin'):
            raise CoreError('Unsupported native reseller operation')
        headers = {'Accept': 'application/json'}
        if path == '/api/admin':
            if not cookie:
                return 401, b'{}', 'application/json'
            headers['Cookie'] = cookie
        else:
            key = str(self.app.config.get('API_KEY') or '').strip()
            if not key:
                raise CoreError('Native API credential is not configured')
            headers['X-API-KEY'] = key
        # A new app context is essential: Flask-Login caches its user on g and
        # SQLAlchemy scopes its session to the app context, not the HTTP request.
        # Reusing the reseller context could leak identity or roll back its work.
        base_url = request.host_url
        with self.app.app_context():
            client = Client(self.app, Response, use_cookies=False)
            response = client.open(path, method=method, json=payload, headers=headers,
                                   base_url=base_url, buffered=False)
            try:
                body = bytearray()
                for chunk in response.iter_encoded():
                    body.extend(chunk)
                    if len(body) > 16 * 1024 * 1024:
                        raise CoreError('Native response exceeds safe size')
                return response.status_code, bytes(body), response.headers.get('Content-Type', '')
            finally:
                response.close()

    def json(self, method, path, payload=None, *, cookie=None):
        code, body, _ = self.call(method, path, payload, cookie=cookie)
        try:
            return code, json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise CoreError('Native API returned an unexpected response') from exc


def install_reseller(app, csrf):
    if 'reseller' in app.blueprints:
        return
    root = Path(app.root_path)
    legacy = root / 'deploy/linux/runtime/resellers.sqlite3'
    configured = os.environ.get('WG_PANEL_RESELLER_DB')
    database = Path(configured) if configured else (
        legacy if legacy.is_file() else Path(app.instance_path) / 'resellers.sqlite3')
    native_sessions = app.session_interface
    native_name = native_sessions.get_cookie_name(app)
    remember_name = app.config.get('REMEMBER_COOKIE_NAME', 'remember_token')
    app.session_interface = ResellerSessions(native_sessions)
    app.wsgi_app = ResellerCookies(app.wsgi_app, native_name, remember_name)
    bp = reseller_blueprint('', database, '', core=NativeApplicationCore(app),
        owner_cookie_reader=lambda: request.environ.get('wgpanel.reseller_owner_cookie', ''),
        native_scheme=True)
    csrf.exempt(bp)
    app.register_blueprint(bp)
