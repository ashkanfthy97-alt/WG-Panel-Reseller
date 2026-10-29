"""Owner-controlled profile and bounded automatic port admission."""
import unittest
import test_native_reseller as fixture
from portal_provision_defaults import ProvisionDefaultsStore
from portal_provisioning import PORT_MIN, PORT_MAX, PortPoolExhausted, child_network, suggest_port


class ProvisionProfileChecks(unittest.TestCase):
    post = fixture.NativeResellerChecks.post
    account = fixture.NativeResellerChecks.account
    login = fixture.NativeResellerChecks.login

    def setUp(self):
        fixture.NativeResellerChecks.setUp(self)

    def update(self, value):
        return self.client.put('/resellers/api/interface-defaults', json=value, headers={
            'Origin': 'http://localhost', 'X-Reseller-CSRF': self.csrf})

    def test_native_owner_only_profile_read_and_write(self):
        path = '/resellers/api/interface-defaults'
        self.assertEqual(self.client.get(path).status_code, 401)
        self.account()
        self.login()
        value = ProvisionDefaultsStore(self.app.instance_path).required()
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.update(value).status_code, 401)
        self.client.get('/fixture-owner')
        current = self.client.get(path)
        self.assertEqual(current.status_code, 200)
        self.assertTrue(current.json['configured'])
        self.assertEqual(current.json['port_range'], {'min': 51825, 'max': 52000})
        value.update(endpoint_host='NEW.EXAMPLE.TEST', dns='9.9.9.9', mtu=1420)
        saved = self.update(value)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json['defaults']['endpoint_host'], 'new.example.test')
        self.assertEqual(ProvisionDefaultsStore(self.app.instance_path).required()['mtu'], 1420)

    def test_fixed_port_and_invalid_policy_rejected_without_overwrite(self):
        self.client.get('/fixture-owner')
        profiles = ProvisionDefaultsStore(self.app.instance_path)
        original = profiles.required()
        invalid = [dict(original, listen_port=51825), dict(original, endpoint_host='vpn.example:51825'),
                   dict(original, network_pool='8.8.0.0/16'), dict(original, child_max_peers=True),
                   dict(original, subnet_prefix=31), dict(original, mtu='wrong')]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(self.update(value).status_code, 400)
                self.assertEqual(profiles.required(), original)

    def test_owner_mutation_requires_csrf_and_origin(self):
        self.client.get('/fixture-owner')
        value = ProvisionDefaultsStore(self.app.instance_path).required()
        self.assertEqual(self.client.put('/resellers/api/interface-defaults', json=value,
            headers={'Origin': 'http://localhost'}).status_code, 403)
        self.assertEqual(self.client.put('/resellers/api/interface-defaults', json=value,
            headers={'Origin': 'http://foreign.example', 'X-Reseller-CSRF': self.csrf}).status_code, 403)

    def test_port_range_inclusive_and_exhaustion(self):
        self.assertEqual((PORT_MIN, PORT_MAX), (51825, 52000))
        self.assertEqual(suggest_port({51820, 51824, 52001}), PORT_MIN)
        self.assertEqual(suggest_port(set(range(PORT_MIN, PORT_MAX))), PORT_MAX)
        with self.assertRaises(PortPoolExhausted):
            suggest_port(set(range(PORT_MIN, PORT_MAX + 1)))

    def test_files_native_rows_and_reservations_are_all_excluded(self):
        from pathlib import Path
        from portal_provisioning import occupied_ports
        root = Path(self.temporary.name)
        (root / 'wg-test.conf').write_text('[Interface]\nListenPort = 51826\n')
        jobs = [{'child_id': 10, 'port': 51828, 'state': 'uncertain'},
                {'child_id': 11, 'port': 51829, 'state': 'pending'}]
        ports = occupied_ports([{'listen_port': 51825}], root, jobs)
        self.assertTrue({51825, 51826, 51828, 51829}.issubset(ports))
        self.assertEqual(suggest_port(ports | {51827}), 51830)

    def test_network_allocator_skips_existing_subnet(self):
        self.assertEqual(child_network([{'address': '10.88.0.1/24'}], '10.88.0.0/16', 24), '10.88.1.1/24')


if __name__ == '__main__':
    unittest.main()
