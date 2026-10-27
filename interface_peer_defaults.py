"""Administrator-managed client defaults, additive to native InterfaceConfig."""
from contextlib import closing
import ipaddress
import json
from pathlib import Path
import re
import sqlite3


FIELDS = {'endpoint', 'allowed_ips', 'dns', 'persistent_keepalive', 'mtu', 'peer_endpoint'}


def validate_defaults(value, parse_endpoint):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError('Only supported interface peer defaults may be changed')
    result = {}
    for field in ('endpoint', 'peer_endpoint'):
        raw = value.get(field) or ''
        if not isinstance(raw, str) or len(raw) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
            raise ValueError('Invalid endpoint')
        result[field] = parse_endpoint(raw)
    raw = value.get('allowed_ips') or '0.0.0.0/0, ::/0'
    if not isinstance(raw, str) or len(raw) > 256:
        raise ValueError('Invalid allowed IPs')
    networks = [str(ipaddress.ip_network(part.strip(), strict=False)) for part in raw.split(',') if part.strip()]
    if not networks:
        raise ValueError('At least one allowed network is required')
    result['allowed_ips'] = ', '.join(dict.fromkeys(networks))
    dns = value.get('dns') or ''
    if not isinstance(dns, str) or len(dns) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in dns):
        raise ValueError('Invalid DNS')
    for item in (part.strip() for part in dns.split(',') if part.strip()):
        try:
            ipaddress.ip_address(item)
        except ValueError:
            if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', item):
                raise ValueError('Invalid DNS address or search domain')
    result['dns'] = dns.strip()
    for field, low, high in (('persistent_keepalive', 0, 65535), ('mtu', 576, 9000)):
        number = value.get(field)
        if number in (None, ''):
            result[field] = None
        elif type(number) is not int or not low <= number <= high:
            raise ValueError('Invalid ' + field)
        else:
            result[field] = number
    return result


class InterfaceDefaultsStore:
    def __init__(self, path):
        self.path = Path(path)

    def _connection(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute('CREATE TABLE IF NOT EXISTS interface_peer_defaults (iface_id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
        connection.commit()
        return connection

    def get(self, iface_id):
        with closing(self._connection()) as connection:
            row = connection.execute('SELECT value FROM interface_peer_defaults WHERE iface_id=?', (iface_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def save(self, iface_id, value):
        with closing(self._connection()) as connection:
            with connection:
                connection.execute('INSERT INTO interface_peer_defaults(iface_id,value) VALUES(?,?) '
                    'ON CONFLICT(iface_id) DO UPDATE SET value=excluded.value', (iface_id, json.dumps(value)))


def install_interface_defaults(app, native):
    from flask import abort, jsonify, request
    from auth import require_api_key_or_login
    from models import InterfaceConfig, db

    store = InterfaceDefaultsStore(Path(app.instance_path) / 'interface-peer-defaults.sqlite3')

    def effective(iface):
        saved = store.get(iface.id)
        # Native fields are authoritative, including changes made through the
        # original endpoint-default/Interface editing controls.
        saved.update(endpoint=native['iface_endpoint_override'](iface),
                     dns=iface.dns or '', mtu=iface.mtu)
        return validate_defaults(saved, native['parse_endpoint_string'])

    @app.route('/api/iface/<int:iid>/peer-defaults', methods=['GET', 'PUT'])
    @require_api_key_or_login
    def portal_interface_peer_defaults(iid):
        iface = db.session.get(InterfaceConfig, iid)
        if iface is None:
            abort(404)
        if iface.node_id is not None:
            return jsonify(error='local_interface_required'), 400
        if request.method == 'GET':
            return jsonify(ok=True, defaults=effective(iface))
        try:
            value = validate_defaults(request.get_json(silent=True), native['parse_endpoint_string'])
            endpoint = value['endpoint']
            if endpoint:
                host, port = native['_host_port'](endpoint)
            else:
                host, port = None, None
            iface.endpoint_host, iface.endpoint_port = host, port
            iface.dns, iface.mtu = value['dns'] or None, value['mtu']
            # No shell/network operations are performed by this policy save.
            db.session.commit()
            try:
                store.save(iid, {key: value[key] for key in ('allowed_ips', 'persistent_keepalive', 'peer_endpoint')})
            except (OSError, sqlite3.Error):
                app.logger.exception('Additional peer defaults could not be persisted for interface %s', iid)
                return jsonify(error='partial_peer_defaults_save', detail=(
                    'Endpoint/DNS/MTU were saved, but additional client defaults could not be saved. '
                    'Reload this interface, resolve its storage error and save the defaults again. / '
                    'Endpoint و DNS و MTU ذخیره شدند، اما سایر پیش‌فرض‌ها ذخیره نشدند؛ '
                    'فرم را دوباره باز کنید، مشکل ذخیره‌سازی را برطرف کنید و دوباره ذخیره کنید.')), 500
            return jsonify(ok=True, defaults=value)
        except ValueError as exc:
            db.session.rollback()
            return jsonify(error='invalid_peer_defaults', detail=str(exc)), 400

    @app.before_request
    def portal_apply_interface_peer_defaults():
        if request.endpoint != 'peers_create' or request.method != 'POST':
            return
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or str(value.get('scope', 'local')).lower() != 'local':
            return
        try:
            iid = int(value.get('iface_id', 0))
        except (TypeError, ValueError):
            return
        iface = db.session.get(InterfaceConfig, iid)
        if iface is None or iface.node_id is not None:
            return
        defaults = effective(iface)
        for key, default in defaults.items():
            if value.get(key) in (None, '') and default not in (None, ''):
                value[key] = default

    return store
