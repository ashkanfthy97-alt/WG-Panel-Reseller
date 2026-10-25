"""Atomic identity-checked local peer actions without resetting paid quotas.

Loaded by the reviewed native patch. The gateway keeps the administrative API
credential server-side; native authentication and product CSRF policy still run.
"""
from flask import abort, jsonify, request
from auth import require_api_key
from models import Peer, db
from sqlalchemy import text
from types import SimpleNamespace


def install_peer_guard(app, csrf, native):
    @app.post('/api/portal/owned-peer-action')
    @csrf.exempt
    @require_api_key
    def portal_owned_peer_action():
        request.max_content_length = 32768
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            abort(400)
        peer_id, key, action = payload.get('id'), payload.get('public_key'), payload.get('action')
        if type(peer_id) is not int or peer_id < 1 or not isinstance(key, str) or len(key) != 44 or action not in {'enable', 'disable'}:
            abort(400)
        db.session.rollback()
        # SQLite has no row locks: reserve its write transaction before identity
        # lookup so another administrator cannot delete/reuse this ID mid-action.
        if db.engine.dialect.name == 'sqlite':
            db.session.execute(text('BEGIN IMMEDIATE'))
        try:
            peer = Peer.query.filter_by(id=peer_id, public_key=key).with_for_update().first()
            if peer is None:
                abort(404)
            if peer.iface is None or peer.iface.node_id is not None or peer.subscription_links:
                return jsonify(error='unsupported_peer_scope'), 409
            # SQL rollback expires ORM objects. Cleanup must never reload an ID
            # which a concurrent native administrator may have replaced.
            runtime_peer = SimpleNamespace(id=peer.id, public_key=peer.public_key,
                address=peer.address, iface=SimpleNamespace(name=peer.iface.name, path=peer.iface.path))
            native['_accumulate_peer_usage'](peer)
            if action == 'enable':
                limit = peer.limit_bytes()
                expiry = native['_effective_expiry_ts'](peer)
                if peer.status == 'blocked' or (limit and peer.used_bytes_total >= limit) or (expiry and expiry <= native['now_ts']()):
                    return jsonify(error='peer_limit_reached'), 409
                # api_enable intentionally resets data and time in upstream.
                # A reseller resume must preserve both; reuse lower-level native
                # WireGuard/config operations instead of that reset endpoint.
                resume_started = True
                native['_wg_enable'](peer)
                peer.status = 'online'
                native['_sync_peer'](peer)
            else:
                native['_wg_disable'](peer)
                peer.status = 'offline'
                peer.bytes_offset = 0  # removed kernel counters restart at zero
            db.session.commit()
            return jsonify(success=True, data_reset=False, timer_reset=False)
        except Exception:
            db.session.rollback()
            # Runtime/config changes cannot join the SQL transaction. Fail closed
            # if a resume partially applied; never silently grant extra access.
            if locals().get('resume_started'):
                native['_wg_disable_quiet'](runtime_peer)
            raise
        finally:
            db.session.rollback()
