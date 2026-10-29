"""Atomic identity-checked local peer actions without resetting paid quotas.

Loaded by the reviewed native patch. The gateway keeps the administrative API
credential server-side; native authentication and product CSRF policy still run.
"""
from flask import abort, jsonify, request
from auth import require_api_key
from models import Peer, db
from sqlalchemy import text
from types import SimpleNamespace
import hashlib
import re


def renewal_values(peer, days, volume_gb, *, now, expiry, anchor):
    """Calculate an additive paid package without resetting usage or identity."""
    if type(days) is not int or not 1 <= days <= 366 or type(volume_gb) is not int or not 1 <= volume_gb <= 100000:
        raise ValueError('Invalid renewal duration or volume')
    old_limit = peer.limit_bytes()
    if peer.unlimited or old_limit is None:
        raise ValueError('Unlimited peers require administrator review')
    limit_mib = (int(old_limit) + 1024**2 - 1) // 1024**2 + volume_gb * 1024
    if limit_mib * 1024**2 > 2**63 - 1:
        raise ValueError('The resulting quota is too large')
    if peer.start_on_first_use and not anchor:
        duration = float(peer.time_limit_days or 0) + days
    else:
        anchor = anchor or now
        duration = (max(now, expiry or now) + days * 86400 - anchor) / 86400
    return {'data_limit_value': limit_mib, 'data_limit_unit': 'Mi', 'time_limit_days': duration}


def _ensure_renewal_receipts(engine):
    # DDL finishes before the Peer transaction starts (MySQL commits DDL).
    # The receipt INSERT and quota changes below share the native transaction.
    with engine.begin() as connection:
        connection.execute(text('''CREATE TABLE IF NOT EXISTS portal_peer_renewals (
            receipt_key VARCHAR(128) PRIMARY KEY, peer_id BIGINT NOT NULL,
            public_key VARCHAR(64) NOT NULL, days INTEGER NOT NULL, volume_gb BIGINT NOT NULL)'''))


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
        if type(peer_id) is not int or peer_id < 1 or not isinstance(key, str) or len(key) != 44 or action not in {'enable', 'disable', 'rename', 'renew', 'delete'}:
            abort(400)
        allowed = {'id', 'public_key', 'action'} | ({'name'} if action == 'rename' else {'days', 'volume_gb', 'request_id'} if action == 'renew' else set())
        if action == 'delete':
            allowed.add('iface_id')
            if type(payload.get('iface_id')) is not int or payload['iface_id'] < 1:
                abort(400)
        if set(payload) - allowed:
            abort(400)
        if action == 'rename':
            name = payload.get('name')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                abort(400)
        if action == 'renew':
            receipt = payload.get('request_id')
            if not isinstance(receipt, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{16,128}', receipt):
                abort(400)
            if type(payload.get('days')) is not int or not 1 <= payload['days'] <= 366 or type(payload.get('volume_gb')) is not int or not 1 <= payload['volume_gb'] <= 100000:
                abort(400)
        db.session.rollback()
        if action == 'renew':
            _ensure_renewal_receipts(db.engine)
        # SQLite has no row locks: reserve its write transaction before identity
        # lookup so another administrator cannot delete/reuse this ID mid-action.
        if db.engine.dialect.name == 'sqlite':
            db.session.execute(text('BEGIN IMMEDIATE'))
        try:
            peer = Peer.query.filter_by(id=peer_id, public_key=key).with_for_update().first()
            if peer is None:
                if action == 'delete' and Peer.query.filter_by(public_key=key).first() is None:
                    # A retry after SQL deletion (or a reused native ID) must
                    # never delete the new occupant of that ID.
                    return jsonify(success=True, absent=True)
                abort(404)
            if peer.iface is None or peer.iface.node_id is not None or peer.subscription_links:
                return jsonify(error='unsupported_peer_scope'), 409
            if action == 'delete':
                if peer.iface_id != payload['iface_id']:
                    return jsonify(error='peer_interface_changed'), 409
                # Native runtime/config removal verifies the kernel key is
                # gone before deleting shortlinks/events/the Peer row. Keep
                # the identity lock until _delete_peer_rows commits.
                native['_remove_local_peer_runtime_and_config'](peer)
                native['_delete_peer_rows'](peer)
                return jsonify(success=True, deleted=True)
            receipt_key = None
            if action == 'renew':
                receipt_key = hashlib.sha256(payload['request_id'].encode('utf-8')).hexdigest()
                previous = db.session.execute(text('SELECT peer_id,public_key,days,volume_gb FROM portal_peer_renewals WHERE receipt_key=:key'), {'key': receipt_key}).mappings().first()
                if previous:
                    if (previous['peer_id'], previous['public_key'], previous['days'], previous['volume_gb']) != (peer_id, key, payload['days'], payload['volume_gb']):
                        return jsonify(error='renewal_receipt_conflict'), 409
                    return jsonify(success=True, replay=True, data_reset=False, timer_reset=False)
            # SQL rollback expires ORM objects. Cleanup must never reload an ID
            # which a concurrent native administrator may have replaced.
            runtime_peer = SimpleNamespace(id=peer.id, public_key=peer.public_key,
                address=peer.address, iface=SimpleNamespace(name=peer.iface.name, path=peer.iface.path))
            native['_accumulate_peer_usage'](peer)
            if action == 'rename':
                peer.name = payload['name'].strip()
                native['_sync_peer'](peer)
            elif action == 'renew':
                now = native['now_ts']()
                expiry = native['_effective_expiry_ts'](peer)
                anchor = native['_timer_anchor_ts'](peer)
                old_limit = peer.limit_bytes()
                was_limited = bool((old_limit and peer.used_bytes_total >= old_limit) or (expiry and expiry <= now))
                try:
                    values = renewal_values(peer, payload['days'], payload['volume_gb'], now=now, expiry=expiry, anchor=anchor)
                except ValueError as exc:
                    return jsonify(error='invalid_renewal', detail=str(exc)), 409
                for field, value in values.items():
                    setattr(peer, field, value)
                if not anchor and not peer.start_on_first_use:
                    peer.timer_started_at = native['from_ts'](now)
                native['_sync_effective_expiry'](peer)
                if peer.status == 'blocked' and was_limited and peer.used_bytes_total < peer.limit_bytes():
                    resume_started = True
                    native['_wg_enable'](peer)
                    peer.status = 'online'
                    peer.bytes_offset = 0
                native['_sync_peer'](peer)
                db.session.execute(text('''INSERT INTO portal_peer_renewals
                    (receipt_key,peer_id,public_key,days,volume_gb) VALUES(:key,:id,:pub,:days,:volume)'''),
                    {'key': receipt_key, 'id': peer_id, 'pub': key, 'days': payload['days'], 'volume': payload['volume_gb']})
            elif action == 'enable':
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
