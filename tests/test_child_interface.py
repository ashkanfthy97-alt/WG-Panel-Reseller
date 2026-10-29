"""Automatic child admission using temporary SQLite and simulated provisioning."""
import unittest
from unittest.mock import patch
import test_native_reseller as fixture
from portal_provision_defaults import ProvisionDefaultsStore
from portal_provisioning import PORT_MIN, PORT_MAX, ProvisionError


class ChildInterfaceChecks(unittest.TestCase):
    post = fixture.NativeResellerChecks.post
    account = fixture.NativeResellerChecks.account
    login = fixture.NativeResellerChecks.login

    def setUp(self):
        fixture.NativeResellerChecks.setUp(self)
        self.parent = self.account()
        self.login()

    def create(self, username='child', **extra):
        return self.post('/reseller/api/accounts', dict(username=username,
                         password='long-test-password', **extra))

    def test_automatic_interface_pending_payment_and_global_profile(self):
        profiles = ProvisionDefaultsStore(self.app.instance_path)
        profile = profiles.required()
        profile.update(endpoint_host='updated.example.test', dns='9.9.9.9', mtu=1420,
                       persistent_keepalive=15, network_pool='172.20.0.0/16', child_max_peers=20)
        profiles.save(profile)
        response = self.create()
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        child = self.store.get(response.json['account']['id'])
        self.assertEqual(child['interfaces'], [3])
        self.assertEqual(child['parent_id'], self.parent['id'])
        self.assertEqual(child['max_peers'], self.parent['max_peers'])
        self.assertFalse(child['active'])
        self.assertEqual(child['interface_provision']['state'], 'complete')
        native = self.native_interface_calls[0]
        for key in ('endpoint_host', 'dns', 'mtu', 'auto_firewall'):
            self.assertEqual(native[key], profile[key], key)
        for key in ('persistent_keepalive', 'allowed_ips'):
            self.assertEqual(native['peer_defaults'][key], profile[key], key)
        self.assertEqual(native['address'], '172.20.0.1/24')
        self.assertEqual(native['listen_port'], PORT_MIN)
        self.assertNotIn('private_key', native)

    def test_used_ports_and_subnets_not_reused(self):
        self.interface_rows.append(dict(id=9, name='occupied', address='10.99.0.1/24', listen_port=PORT_MIN))
        first, second = self.create('first-child'), self.create('second-child')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual([row['listen_port'] for row in self.native_interface_calls], [PORT_MIN + 1, PORT_MIN + 2])
        self.assertNotEqual(first.json['interface']['address'], second.json['interface']['address'])

    def test_pool_exhaustion_precedes_account_creation(self):
        with patch('portal_provisioning.occupied_ports', return_value=set(range(PORT_MIN, PORT_MAX + 1))):
            response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error'], 'port_pool_exhausted')
        self.assertEqual(len(self.store.all()), 1)
        self.assertFalse(self.native_interface_calls)

    def test_scan_failure_missing_profile_precede_billing(self):
        with patch('portal_provisioning.occupied_ports', side_effect=ProvisionError('simulated scan failure')):
            self.assertEqual(self.create().status_code, 503)
        ProvisionDefaultsStore(self.app.instance_path).path.unlink()
        response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error'], 'provision_defaults_required')
        self.assertEqual(len(self.store.all()), 1)
        self.assertFalse(self.native_interface_calls)

    def test_uncertain_outcome_blocks_duplicate_retry_and_activation(self):
        self.native_interface_error = 201, {'interface': {'id': 3}, 'up_error': 'simulated failure'}
        response = self.create()
        self.assertEqual(response.status_code, 502)
        child = self.store.get(response.json['account']['id'])
        self.assertEqual(child['interface_provision']['state'], 'uncertain')
        self.assertEqual(child['interfaces'], [])
        self.assertFalse(child['active'])
        self.assertEqual(self.create().status_code, 400)
        self.assertEqual(len(self.native_interface_calls), 1)
        self.assertEqual(self.post('/reseller/api/accounts/' + str(child['id']) + '/interface', {}).status_code, 400)
        self.client.get('/fixture-owner')
        approval = self.post('/resellers/api/accounts/' + str(child['id']) + '/approval', {'approved': True})
        self.assertEqual(approval.status_code, 409)
        self.assertEqual(approval.json['error'], 'interface_review_required')

    def test_parent_cannot_override_admin_settings(self):
        for extra in ({'max_peers': 2}, {'port': PORT_MIN}, {'interfaces': [2]},
                      {'address': '10.0.0.1/24'}, {'endpoint_host': 'other.example'}):
            with self.subTest(extra=extra):
                self.assertEqual(self.create(**extra).status_code, 400)
        self.assertFalse(self.native_interface_calls)
        self.assertEqual(len(self.store.all()), 1)


if __name__ == '__main__':
    unittest.main()
