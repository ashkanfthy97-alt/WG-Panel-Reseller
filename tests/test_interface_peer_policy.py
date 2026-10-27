"""Temporary policy/auth tests and ORM-independent package calculations."""
import ast
import importlib.util
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from flask import Flask

import test_native_reseller as fixture
from interface_peer_defaults import InterfaceDefaultsStore, validate_defaults, install_interface_defaults

ROOT = Path(__file__).resolve().parents[1]


class DefaultsChecks(unittest.TestCase):
    def parse_endpoint(self, value):
        if value and (':' not in value or not value.rsplit(':', 1)[1].isdigit()):
            raise ValueError('invalid endpoint')
        return value

    def test_policy_validation_and_persistence(self):
        value = validate_defaults(dict(endpoint='vpn.example.test:51820', dns='1.1.1.1',
            allowed_ips='10.1.2.8/24, ::/0', mtu=1380, persistent_keepalive=25, peer_endpoint=''), self.parse_endpoint)
        self.assertEqual(value['allowed_ips'], '10.1.2.0/24, ::/0')
        with tempfile.TemporaryDirectory() as directory:
            store = InterfaceDefaultsStore(Path(directory) / 'policy.sqlite3')
            store.save(7, value)
            self.assertEqual(store.get(7), value)
            self.assertEqual(store.get(8), {})

    def test_invalid_technical_values_are_rejected(self):
        for payload in ({'endpoint':'invalid'}, {'allowed_ips':'not-a-network'}, {'dns':'bad\nvalue'},
                        {'mtu':20}, {'persistent_keepalive':True}, {'private_key':'forged'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_defaults(payload, self.parse_endpoint)

    def renewal(self, peer, **values):
        tree = ast.parse((ROOT / 'portal_peer_guard.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'renewal_values')
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'renewal', 'exec'), namespace)
        return namespace['renewal_values'](peer, **values)

    def test_renewal_adds_quota_and_time_without_resetting_usage(self):
        peer = SimpleNamespace(unlimited=False, start_on_first_use=False, time_limit_days=10,
                               used_bytes_total=3 * 1024**3, private_key='preserved', limit_bytes=lambda:10*1024**3)
        result = self.renewal(peer, days=5, volume_gb=7, now=86400, expiry=10*86400, anchor=1)
        self.assertEqual(result['data_limit_value']*1024**2, 17*1024**3)
        self.assertAlmostEqual(result['time_limit_days'], 15-1/86400)
        self.assertEqual(peer.used_bytes_total, 3*1024**3)
        self.assertEqual(peer.private_key, 'preserved')
        self.assertEqual(set(result), {'data_limit_value','data_limit_unit','time_limit_days'})

    def test_unused_first_use_timer_stays_unstarted(self):
        peer = SimpleNamespace(unlimited=False, start_on_first_use=True, time_limit_days=10, limit_bytes=lambda:1024**3)
        result = self.renewal(peer, days=5, volume_gb=1, now=900000, expiry=None, anchor=None)
        self.assertEqual(result['time_limit_days'], 15)

    def test_partial_storage_failure_is_reported_explicitly(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            app=Flask('policy-fixture',instance_path=directory)
            app.config['TESTING']=True
            iface=SimpleNamespace(id=1,node_id=None,endpoint_host=None,endpoint_port=None,dns='',mtu=None)
            session=SimpleNamespace(get=Mock(return_value=iface),commit=Mock(),rollback=Mock())
            modules={'auth':SimpleNamespace(require_api_key_or_login=lambda fn:fn),
                     'models':SimpleNamespace(InterfaceConfig=object,db=SimpleNamespace(session=session))}
            native={'parse_endpoint_string':self.parse_endpoint,'_host_port':lambda endpoint:(endpoint.rsplit(':',1)[0],int(endpoint.rsplit(':',1)[1])),
                    'iface_endpoint_override':lambda _:''}
            with patch.dict(sys.modules,modules):
                store=install_interface_defaults(app,native)
            with patch.object(store,'save',side_effect=sqlite3.OperationalError('simulated full disk')), patch.object(app.logger,'exception'):
                response=app.test_client().put('/api/iface/1/peer-defaults',json={'endpoint':'vpn.test:51820','dns':'1.1.1.1','mtu':1380})
            self.assertEqual(response.status_code,500)
            self.assertEqual(response.json['error'],'partial_peer_defaults_save')
            self.assertIn('were saved',response.json['detail'])
            session.commit.assert_called_once()


class RestrictedResellerChecks(unittest.TestCase):
    setUp = fixture.NativeResellerChecks.setUp
    post = fixture.NativeResellerChecks.post
    login = fixture.NativeResellerChecks.login
    account = fixture.NativeResellerChecks.account

    def test_server_applies_assigned_interface_defaults(self):
        self.account()
        self.login()
        result = self.post('/reseller/api/peers', {'name':'customer','days':30,'volume_gb':10})
        self.assertEqual(result.status_code, 201, result.get_data(as_text=True))
        self.assertEqual(self.rows[0]['iface_id'], 1)
        self.assertEqual(self.client.get('/reseller/api/peers').json['assigned_interface']['id'], 1)

    def test_reseller_cannot_override_interface_or_technical_values(self):
        self.account()
        self.login()
        for key, value in [('iface_id',1),('endpoint','evil.test:42'),('allowed_ips','10.0.0.0/8'),('dns','8.8.8.8'),('mtu',9000),('persistent_keepalive',0),('peer_endpoint','evil.test:43')]:
            result = self.post('/reseller/api/peers', dict(name='customer', days=30, volume_gb=10, **{key:value}))
            self.assertIn(result.status_code, (400,403))
        self.assertEqual(self.rows, [])

    def test_legacy_multiple_interfaces_require_admin_choice(self):
        account = self.store.create('dealer','a-safe-test-password',[1,2],3)
        self.store.update(account['id'], days=30)
        self.login()
        result = self.post('/reseller/api/peers', dict(name='customer',days=30,volume_gb=10))
        self.assertIn(result.status_code, (400,409))
        self.assertEqual(self.rows, [])

    def test_assignment_is_mother_only(self):
        account = self.account()
        self.login()
        url = '/resellers/api/accounts/' + str(account['id']) + '/interface'
        self.assertEqual(self.post(url, {'iface_id':2}).status_code, 401)
        self.client.get('/fixture-owner')
        result = self.post(url, {'iface_id':2})
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.assertEqual(self.store.get(account['id'])['interfaces'], [2])


if __name__ == '__main__':
    unittest.main()
