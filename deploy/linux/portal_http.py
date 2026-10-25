"""HTTP-01 challenge responder; port 80 is never a general-purpose proxy."""

from __future__ import annotations

import os
import html
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, redirect, request, send_from_directory


WEBROOT = Path(os.environ.get("WG_PORTAL_ACME_WEBROOT", "./runtime/acme-webroot")).resolve()
PUBLIC_IP = os.environ.get("WG_PORTAL_PUBLIC_IP", "").strip()
RUNTIME = Path(os.environ.get("WG_PORTAL_RUNTIME", Path(__file__).parent / "runtime")).resolve()
SETUP_DB = Path(os.environ.get("WG_PORTAL_SETUP_DB", RUNTIME / "setup.sqlite3"))
PENDING_HOST = RUNTIME / "acme-pending-host"
HTTPS_PORT = int(os.environ.get("WG_PORTAL_HTTPS_PORT", "8767"))
ALLOWED_HOSTS = {
    value.strip().lower().rstrip(".")
    for value in os.environ.get("WG_PORTAL_ALLOWED_HOSTS", PUBLIC_IP).split(",")
    if value.strip()
}
app = Flask(__name__, static_folder=None)


def _active_domain() -> str:
    if not SETUP_DB.is_file():
        return ""
    try:
        with sqlite3.connect(f"file:{SETUP_DB.as_posix()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT domain, tls_ready, cert_name FROM setup_state WHERE id=1").fetchone()
        return str(row[0] or "").lower().rstrip(".") if row and row[1] and row[2] == "wg-portal-domain" else ""
    except sqlite3.Error:
        return ""


def _pending_challenge_host() -> str:
    if not request.path.startswith("/.well-known/acme-challenge/"):
        return ""
    try:
        candidate = json.loads(PENDING_HOST.read_text(encoding="utf-8"))
        if float(candidate.get("expires_at", 0)) > time.time():
            return str(candidate.get("host", "")).lower().rstrip(".")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return ""


def _tls_address() -> str:
    host = f"[{PUBLIC_IP}]" if ":" in PUBLIC_IP else PUBLIC_IP
    return f"https://{host}:{HTTPS_PORT}"


@app.before_request
def validate_host():
    host = (urlsplit("//" + request.host).hostname or "").lower().rstrip(".")
    if host not in (ALLOWED_HOSTS | {_active_domain(), _pending_challenge_host()}):
        abort(400)


@app.get("/.well-known/acme-challenge/<path:token>")
def challenge(token: str):
    if not token or "/" in token or "\\" in token:
        abort(404)
    return send_from_directory(WEBROOT / ".well-known" / "acme-challenge", token, mimetype="text/plain", max_age=0)


@app.get("/")
def welcome():
    if not PUBLIC_IP:
        return "Public IP is not configured", 503
    target = html.escape(_tls_address() + "/", quote=True)
    return Response(
        '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow"><title>WG Portal</title>'
        '<body><main><h1>به پنل وایرگارد خوش آمدید</h1>'
        '<p>برای راه‌اندازی و ورود، اتصال رمزنگاری‌شده را باز کنید. در این صفحه هیچ کد نصب یا رمزی وارد نکنید.</p>'
        '<p>در نصب نخست، گواهی محلی است و مرورگر هشدار می‌دهد؛ اثرانگشت گواهی را با خروجی نصب سرور تطبیق دهید.</p>'
        f'<p><a href="{target}">ادامه با HTTPS / Continue securely</a></p>'
        '<section lang="en" dir="ltr"><h2>Welcome to WG Portal</h2>'
        '<p>Open the encrypted connection to set up or sign in. Never enter a token or password on HTTP.</p>'
        '<p>The first-run certificate is self-signed. Compare its fingerprint with the server installer output before proceeding.</p>'
        '</section></main></body></html>', content_type="text/html; charset=utf-8",
    )


@app.route("/<path:path>", methods=["GET", "HEAD"])
def redirect_to_tls(path: str):
    if not PUBLIC_IP:
        return "Public IP is not configured", 503
    return redirect(_tls_address() + "/setup", code=308)


@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    return response
