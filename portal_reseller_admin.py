"""Explicit native administrator-bot bridge, isolated from reseller dispatch."""
import hmac
import re
from flask import abort, current_app, g, jsonify, make_response, request
from werkzeug.test import Client
from werkzeug.wrappers import Response


def install_admin_bridge(bp, store, handlers, account_details):
    @bp.post('/api/portal/reseller-admin')
    def reseller_admin_bridge():
        key = str(current_app.config.get('API_KEY') or '')
        supplied = request.headers.get('X-API-KEY', '')
        if not key or not supplied or not hmac.compare_digest(key, supplied):
            return jsonify(error='native_api_key_required'), 401
        if request.content_length and request.content_length > 32768:
            abort(413)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {'action','payload','actor','request_id'}:
            abort(400)
        actor = body.get('actor')
        if not isinstance(actor, dict) or set(actor) - {'id','username'}:
            abort(400)
        actor_id = str(actor.get('id') or '')
        if not re.fullmatch(r'[1-9][0-9]{0,19}', actor_id):
            abort(400)
        # Require a current native Telegram administrator as well as the server
        # credential. No browser cookie, supplied identity header or reseller
        # role selector can authorize this path.
        app = current_app._get_current_object()
        with app.app_context():
            result = Client(app, Response, use_cookies=False).get('/api/telegram/admins',
                headers={'X-API-KEY': key}, base_url=request.host_url)
            try:
                admins = result.get_json(silent=True) if result.status_code == 200 else None
            finally:
                result.close()
        registered = admins.get('admins') if isinstance(admins, dict) else None
        if not isinstance(registered, list) or actor_id not in {str(row.get('id')) for row in registered if isinstance(row, dict)}:
            return jsonify(error='telegram_admin_required'), 403
        audit_actor = 'telegram-admin:' + actor_id
        action = body.get('action')
        if not isinstance(action, str) or len(action) > 32:
            abort(400)
        if not isinstance(body.get('request_id', ''), str):
            abort(400)
        payload = body.get('payload', {})
        if not isinstance(payload, dict):
            abort(400)
        if action in {'detail','quota','reset2fa'}:
            account_id = payload.get('id')
            if type(account_id) is not int or account_id < 1:
                abort(400)
            account = store.get(account_id)
            if account is None:
                abort(404)
            if action == 'detail':
                if set(payload) != {'id'}:
                    abort(400)
                return jsonify(account=account_details([account])[0],
                    audit=[row for row in store.audit() if row['reseller_id']==account_id])
            if action == 'quota':
                if set(payload) != {'id','max_peers'}:
                    abort(400)
                return jsonify(account=store.set_quota(account_id,payload['max_peers'],audit_actor))
            if set(payload) != {'id','confirm'} or payload.get('confirm') is not True:
                abort(400)
            request_id = body.get('request_id', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
                abort(400)
            from portal_reseller_security import reset
            return jsonify(account=reset(store,account_id,audit_actor,request_id=request_id))
        if action not in handlers:
            abort(400)
        function, path, method = handlers[action]
        kwargs = {}
        if '{id}' in path:
            account_id = payload.get('id')
            if type(account_id) is not int or account_id < 1:
                abort(400)
            kwargs['account_id'] = account_id
            path = path.format(id=account_id)
            payload = {name:value for name,value in payload.items() if name!='id'}
        if action in {'approve','revoke'}:
            if set(payload)-{'days'}:
                abort(400)
            payload = {**payload,'approved':action=='approve'}
        elif action in {'enable','disable'}:
            if payload:
                abort(400)
            payload = {'enabled':action=='enable'}
        elif action in {'list','defaults_get'} and payload:
            abort(400)
        request_id = body.get('request_id','')
        if action == 'renew' and not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', str(request_id)):
            abort(400)
        # Invoke the same private owner service bodies, with an internal request
        # context for validated JSON. No native owner cookie/session is created,
        # and the browser require_owner decorator remains cookie-only.
        with app.test_request_context(path,method=method,json=payload,
                headers={'Idempotency-Key':request_id} if request_id else {}):
            g.reseller_owner = audit_actor
            response = make_response(function.__wrapped__(**kwargs))
            value = response.get_json(silent=True)
            status = response.status_code
        if not isinstance(value,dict):
            return jsonify(error='invalid_admin_response'), 502
        value.pop('csrf',None)
        value.pop('native_csrf',None)
        return jsonify(value), status
