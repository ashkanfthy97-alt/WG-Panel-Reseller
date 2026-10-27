"""Native Interface provisioning with shared, process-safe port admission."""
from contextlib import contextmanager
from functools import wraps
import ipaddress
import json
import os
from pathlib import Path
import re
import threading

from flask import current_app, g, jsonify, request

_threads = threading.RLock()
_local = threading.local()


class ProvisionError(ValueError):
    pass


@contextmanager
def provisioning_lock(instance_path):
    """Serialize native owner and child creation, including nested WSGI calls."""
    with _threads:
        if getattr(_local, 'depth', 0):
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        target = Path(instance_path)
        target.mkdir(parents=True, exist_ok=True)
        with (target / 'interface-provision.lock').open('a+b') as handle:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            else:
                import msvcrt
                handle.seek(0)
                handle.write(b'0')
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            _local.depth = 1
            try:
                yield
            finally:
                _local.depth = 0
                if os.name == 'posix':
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def system_ports(proc_root='/proc/net'):
    """Read all TCP/UDP bindings, including IPv6 and unconnected UDP sockets."""
    ports = set()
    for protocol in ('tcp', 'tcp6', 'udp', 'udp6'):
        path = Path(proc_root) / protocol
        try:
            lines = path.read_text(encoding='ascii').splitlines()[1:]
            for line in lines:
                fields = line.split()
                ports.add(int(fields[1].rsplit(':', 1)[1], 16))
        except (OSError, ValueError, IndexError) as exc:
            raise ProvisionError('بررسی پورت‌های سیستم کامل نشد؛ ایجاد متوقف شد.') from exc
    return ports


def occupied_ports(rows, config_path, reservations=(), *, exclude_child=None, scanner=system_ports):
    ports = set(scanner())
    for row in rows:
        value = row.get('listen_port') if isinstance(row, dict) else getattr(row, 'listen_port', None)
        if value:
            ports.add(int(value))
    root = Path(config_path)
    if root.is_file():
        root = root.parent
    try:
        if root.exists():
            for path in root.glob('*.conf'):
                for value in re.findall(r'^\s*ListenPort\s*=\s*(\d+)\s*(?:#.*)?$', path.read_text(), re.M | re.I):
                    ports.add(int(value))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProvisionError('بررسی کانفیگ‌های Interface کامل نشد.') from exc
    for job in reservations:
        if exclude_child is not None and job.get('child_id') == exclude_child:
            continue
        if job.get('state') in {'pending', 'complete', 'uncertain'}:
            ports.add(int(job['port']))
    return ports


def suggest_port(occupied):
    for port in range(20000, 65536):
        if port not in occupied:
            return port
    raise ProvisionError('پورت آزاد موجود نیست.')


def child_network(rows):
    networks = []
    for row in rows:
        for network in row.get('scope_networks', []):
            try:
                networks.append(ipaddress.ip_network(network, strict=False))
            except (ValueError, TypeError) as exc:
                raise ProvisionError('شبکهٔ محلی سیستم نامعتبر است.') from exc
        for address in re.split(r'[\s,]+', str(row.get('address') or '')):
            if address:
                try:
                    networks.append(ipaddress.ip_interface(address).network)
                except ValueError as exc:
                    raise ProvisionError('شبکهٔ Interface موجود نامعتبر است.') from exc
    for subnet in ipaddress.ip_network('10.0.0.0/8').subnets(new_prefix=24):
        if not any(subnet.version == existing.version and subnet.overlaps(existing) for existing in networks):
            return str(subnet.network_address + 1) + '/24'
    raise ProvisionError('شبکهٔ آزاد موجود نیست.')


def interface_port_guard(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        from models import InterfaceConfig
        from deploy.linux.reseller_store import ResellerStore
        with provisioning_lock(current_app.instance_path):
            value = request.get_json(silent=True)
            if not isinstance(value, dict):
                try:
                    value = json.loads(request.get_data(cache=True, as_text=True).strip())
                except (ValueError, TypeError):
                    value = None
            if not isinstance(value, dict):
                return fn(*args, **kwargs)
            try:
                port = int(value.get('listen_port'))
            except (ValueError, TypeError):
                return fn(*args, **kwargs)  # Native validation returns the precise error.
            try:
                database = current_app.extensions.get('portal_reseller_database')
                jobs = ResellerStore(database).interface_reservations() if database else []
                occupied = occupied_ports(InterfaceConfig.query.all(),
                    current_app.config.get('WG_CONF_PATH') or '/etc/wireguard', jobs,
                    exclude_child=getattr(g, 'portal_reserved_child', None))
                if port in occupied:
                    return jsonify(ok=False, error='listen_port_in_use',
                        detail='این پورت قبلاً استفاده یا رزرو شده است.'), 409
            except Exception:
                current_app.logger.exception('Interface port admission failed')
                return jsonify(ok=False, error='port_scan_failed'), 503
            return fn(*args, **kwargs)
    return guarded


def install_provisioning(app, csrf, scope):
    """API-key-only bridge to the original creation body, never an owner login."""
    import hmac
    def provision():
        wanted = str(app.config.get('API_KEY') or '')
        supplied = request.headers.get('X-API-KEY', '')
        if not wanted or not hmac.compare_digest(wanted, supplied):
            return jsonify(error='Unauthorized'), 401
        value = request.get_json(silent=True) or {}
        if type(value.get('child_id')) is not int:
            return jsonify(error='invalid_child'), 400
        g.portal_reserved_child = value['child_id']
        response = app.make_response(scope['create_local_interface'].__wrapped__())
        body = response.get_json(silent=True) or {}
        if response.status_code == 201 and not body.get('up_error'):
            iface = scope['InterfaceConfig'].query.get(body['interface']['id'])
            # The parent's endpoint host is reusable; its old port is not.
            iface.endpoint_host = value.get('endpoint_host') or None
            iface.endpoint_port = iface.listen_port if iface.endpoint_host else None
            scope['db'].session.commit()
            from interface_peer_defaults import InterfaceDefaultsStore
            InterfaceDefaultsStore(Path(app.instance_path) / 'interface-peer-defaults.sqlite3').save(
                iface.id, value.get('peer_defaults') or {})
        return response
    app.add_url_rule('/api/portal/provision-child-interface', 'portal_provision_child_interface', provision, methods=['POST'])
    csrf.exempt(provision)
