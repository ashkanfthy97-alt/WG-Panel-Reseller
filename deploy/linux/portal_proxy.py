"""Local-only frontend and gateway for the native WG_Panel service."""

import os
from pathlib import Path

import requests
from flask import Flask, Response, abort, request, send_from_directory


SITE_ROOT = Path(os.environ["WG_PORTAL_SITE_ROOT"]).resolve()
PANEL_URL = os.environ["WG_PORTAL_PANEL_URL"].rstrip("/")
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

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024


def static_file(name):
    response = send_from_directory(SITE_ROOT, name)
    response.headers.update(STATIC_HEADERS)
    return response


@app.route("/")
def home():
    return static_file("index.html")


@app.route("/app.html")
def dashboard():
    return static_file("app.html")


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
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_HEADERS
        and key.lower() not in {"host", "content-length", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}
    }
    headers["Host"] = request.host
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-For"] = request.remote_addr or "127.0.0.1"
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    headers["X-Forwarded-Proto"] = proto if proto in {"http", "https"} else request.scheme
    body = request.get_data(cache=False) if request.method in {"POST", "PUT", "PATCH", "DELETE"} else None
    try:
        upstream = requests.request(
            request.method,
            url,
            headers=headers,
            data=body,
            stream=True,
            allow_redirects=False,
            timeout=(5, 120),
        )
    except requests.RequestException:
        app.logger.exception("WG_Panel upstream unavailable")
        return Response("WG_Panel is unavailable", status=502, content_type="text/plain")
    response_headers = []
    for key in upstream.raw.headers.keys():
        if key.lower() in HOP_HEADERS:
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
    return response
