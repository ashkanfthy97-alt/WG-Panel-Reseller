"""Mother-admin policy for newly provisioned reseller Interfaces only."""
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile

from interface_peer_defaults import validate_defaults
from portal_provisioning import provisioning_lock, PORT_MIN, PORT_MAX


class ProvisionDefaultsError(ValueError):
    pass


class ProvisionDefaultsMissing(ProvisionDefaultsError):
    pass


DEFAULTS = {
    'endpoint_host': '', 'dns': '1.1.1.1, 1.0.0.1', 'mtu': 1380,
    'allowed_ips': '0.0.0.0/0, ::/0', 'persistent_keepalive': 25,
    'network_pool': '10.0.0.0/8', 'subnet_prefix': 24,
    'child_max_peers': 100, 'auto_firewall': True,
}


def validate_profile(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ProvisionDefaultsError('همهٔ پیش‌فرض‌های مجاز را مشخص کنید؛ پورت فقط خودکار تعیین می‌شود.')
    host = value['endpoint_host']
    if not isinstance(host, str) or not host.strip() or len(host) > 255:
        raise ProvisionDefaultsError('میزبان Endpoint بدون پورت الزامی است.')
    host = host.strip()
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                                      for label in host.split('.')):
            raise ProvisionDefaultsError('Endpoint باید نام میزبان یا IP، بدون پورت و نشانی وب باشد.') from None
        host = host.lower()
    try:
        policy = validate_defaults({key: value[key] for key in
            ('dns', 'mtu', 'allowed_ips', 'persistent_keepalive')}, lambda raw: raw)
        if not isinstance(value['network_pool'], str):
            raise ValueError('network pool')
        pool = ipaddress.ip_network(value['network_pool'].strip(), strict=True)
        private = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
        if pool.version != 4 or not any(pool.subnet_of(ipaddress.ip_network(item)) for item in private):
            raise ValueError('private IPv4 pool required')
        prefix = value['subnet_prefix']
        maximum = value['child_max_peers']
        if type(prefix) is not int or not max(16, pool.prefixlen) <= prefix <= 30:
            raise ValueError('subnet prefix')
        if type(maximum) is not int or not 1 <= maximum <= min(10000, 2 ** (32-prefix) - 3):
            raise ValueError('peer capacity')
        if type(value['auto_firewall']) is not bool:
            raise ValueError('auto firewall')
        if policy['mtu'] is None or policy['persistent_keepalive'] is None:
            raise ValueError('MTU and keepalive required')
    except (ValueError, TypeError) as error:
        raise ProvisionDefaultsError('DNS، MTU، مسیرها، محدودهٔ شبکه، اندازهٔ زیرشبکه و سقف Peer را بررسی کنید.') from error
    return {'endpoint_host': host, **{key: policy[key] for key in
        ('dns', 'mtu', 'allowed_ips', 'persistent_keepalive')},
        'network_pool': str(pool), 'subnet_prefix': prefix,
        'child_max_peers': maximum, 'auto_firewall': value['auto_firewall']}


class ProvisionDefaultsStore:
    def __init__(self, instance_path):
        self.instance = Path(instance_path)
        self.path = self.instance / 'reseller-interface-defaults.json'

    def load(self):
        try:
            raw = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return self.response(False, dict(DEFAULTS))
        except OSError as error:
            raise ProvisionDefaultsError('خواندن پیش‌فرض‌های Interface ممکن نشد.') from error
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('version') != 1:
                raise ValueError('unsupported profile')
            defaults = validate_profile(data.get('defaults'))
        except (ValueError, TypeError) as error:
            raise ProvisionDefaultsError('پیش‌فرض‌های ذخیره‌شده معتبر نیستند؛ مدیر تنظیمات را بررسی کند.') from error
        return self.response(True, defaults)

    @staticmethod
    def response(configured, defaults):
        return {'configured': configured, 'defaults': defaults,
                'port_range': {'min': PORT_MIN, 'max': PORT_MAX}}

    def required(self):
        value = self.load()
        if not value['configured']:
            raise ProvisionDefaultsMissing('مدیر اصلی ابتدا باید پیش‌فرض ساخت Interface زیرنماینده را تنظیم کند.')
        return value['defaults']

    def save(self, value):
        defaults = validate_profile(value)
        with provisioning_lock(self.instance):
            pending = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.reseller-defaults-',
                        suffix='.json', dir=self.instance, delete=False) as handle:
                    pending = Path(handle.name)
                    if os.name != 'nt':
                        os.fchmod(handle.fileno(), 0o600)
                    json.dump({'version': 1, 'defaults': defaults}, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(pending, self.path)
            except OSError as error:
                raise ProvisionDefaultsError('ذخیرهٔ پیش‌فرض‌های Interface کامل نشد.') from error
            finally:
                if pending is not None and pending.exists():
                    pending.unlink()
        return self.response(True, defaults)
