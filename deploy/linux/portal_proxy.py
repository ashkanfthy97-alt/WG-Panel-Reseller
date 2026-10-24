"""Local-only frontend and gateway for the native WG_Panel service."""

import os
import ipaddress
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, abort, redirect, request, send_from_directory


SITE_ROOT = Path(os.environ["WG_PORTAL_SITE_ROOT"]).resolve()
PANEL_URL = os.environ["WG_PORTAL_PANEL_URL"].rstrip("/")
# Only local reverse proxies can supply forwarding metadata. They must overwrite
# X-Forwarded-Proto and append the actual client address to X-Forwarded-For.
PROXY_HEADERS = {"forwarded", "cf-connecting-ip", "cf-visitor", "true-client-ip", "x-real-ip", "x-url-scheme"}
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self' data:; connect-src 'self'"
    ),
}

# /static belongs to WG_Panel. Flask's default static route would shadow it.
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024
app.config.update(SECRET_KEY=os.environ.get('FLASK_SECRET_KEY'),
                  SESSION_COOKIE_NAME='wgportal_reseller_session',
                  SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(minutes=30))
if os.environ.get('WG_PORTAL_RESELLER_DB'):
    if len(os.environ.get('FLASK_SECRET_KEY', '')) < 32:
        raise RuntimeError('A protected application secret is required for reseller sessions')
    from portal_reseller import reseller_blueprint
    app.register_blueprint(reseller_blueprint(PANEL_URL, os.environ['WG_PORTAL_RESELLER_DB'], os.environ.get('API_KEY', '')))


def _allowed_hosts() -> set[str]:
    configured = app.config.get("WG_PORTAL_ALLOWED_HOSTS")
    if configured is None:
        configured = os.environ.get("WG_PORTAL_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
    return {item.strip().lower().rstrip(".") for item in str(configured).split(",") if item.strip()}


@app.before_request
def validate_host():
    host = (urlsplit(request.host_url).hostname or "").lower().rstrip(".")
    if not host or host not in _allowed_hosts():
        abort(400, description="Unrecognized Host header")


def static_file(name):
    response = send_from_directory(SITE_ROOT, name)
    response.headers.update(STATIC_HEADERS)
    return response


@app.route("/")
def home():
    return panel("")


@app.route("/app.html")
def dashboard():
    return redirect('/')


@app.route('/welcome')
def welcome():
    return static_file('index.html')


@app.route("/assets/<path:name>")
def assets(name):
    return static_file("assets/" + name)


@app.route("/<path:path>", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def panel(path):
    if path.startswith("assets/"):
        abort(404)
    url = PANEL_URL + "/" + path
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")
    connection_headers = {name.strip().lower() for name in request.headers.get("Connection", "").split(",")}
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_HEADERS
        and key.lower() not in connection_headers
        and key.lower() not in {"host", "content-length"}
        and key.lower() not in PROXY_HEADERS
        and not key.lower().startswith("x-forwarded-")
    }
    headers["Host"] = request.host
    headers["X-Forwarded-Host"] = request.host
    client_ip = request.remote_addr or "127.0.0.1"
    try:
        trusted = ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        try:
            client_ip = str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    headers["X-Forwarded-For"] = client_ip
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).strip() if trusted else request.scheme
    headers["X-Forwarded-Proto"] = proto if proto in {"http", "https"} else request.scheme
    body = request.get_data(cache=False) if request.method in {"POST", "PUT", "PATCH", "DELETE"} else None
    try:
        transport = requests.Session()
        transport.trust_env = False
        upstream = transport.request(
            request.method,
            url,
            headers=headers,
            data=body,
            stream=True,
            allow_redirects=False,
            timeout=(5, 120),
        )
    except requests.RequestException:
        transport.close()
        app.logger.exception("WG_Panel upstream unavailable")
        return Response("WG_Panel is unavailable", status=502, content_type="text/plain")
    response_headers = []
    response_connection_headers = {name.strip().lower() for name in upstream.headers.get("Connection", "").split(",")}
    for key in upstream.raw.headers.keys():
        if key.lower() in HOP_HEADERS or key.lower() in response_connection_headers:
            continue
        for value in upstream.raw.headers.getlist(key):
            response_headers.append((key, value))
    response = Response(
        upstream.raw.stream(64 * 1024, decode_content=False),
        status=upstream.status_code,
        headers=response_headers,
        direct_passthrough=True,
    )
    response.call_on_close(upstream.close)
    response.call_on_close(transport.close)
    return response
