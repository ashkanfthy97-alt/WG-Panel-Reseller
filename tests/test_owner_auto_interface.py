"""Owner and nested automatic provisioning with fake native IO and temp SQLite."""
from pathlib import Path
import unittest
from unittest.mock import patch

import test_native_reseller as fixture
from deploy.linux.reseller_store import ResellerStore, StoreError
from portal_provision_defaults import ProvisionDefaultsStore
from portal_provisioning import PortPoolExhausted, NetworkPoolExhausted


class OwnerAutomaticInterfaceChecks(unittest.TestCase):
    setUp = fixture.NativeResellerChecks.setUp
    post = fixture.NativeResellerChecks.post
    account = fixture.NativeResellerChecks.account
    login = fixture.NativeResellerChecks.login

    def create_owner(self, username='dealer', **changes):
        self.client.get('/fixture-owner')
        value = {'username': username, 'password': 'a-safe-test-password', 'max_peers': 3}
        value.update(changes)
        return self.post('/resellers/api/accounts', value)

    def test_owner_create_automatically_attaches_without_fake_parent_or_activation(self):
        result = self.create_owner()
        self.assertEqual(result.status_code, 201, result.json)
        account = result.json['account']
        self.assertIsNone(account['parent_id'])
        self.assertFalse(account['active'])
        self.assertEqual(account['approval_status'], 'pending')
        self.assertEqual(account['interfaces'], [3])
        self.assertEqual(account['max_peers'], 3)
        self.assertEqual(account['interface_provision']['kind'], 'owner')
        self.assertIsNone(account['interface_provision']['parent_id'])
        self.assertEqual(result.json['interface']['listen_port'], 51825)
        payload = self.native_interface_calls[0]
        policy = ProvisionDefaultsStore(self.app.instance_path).required()
        for field in ('dns', 'mtu', 'endpoint_host', 'auto_firewall'):
            self.assertEqual(payload[field], policy[field])
        self.assertEqual(payload['peer_defaults'], {key: policy[key] for key in ('allowed_ips', 'persistent_keepalive')})
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reseller_interface_jobs').fetchone()[0], 0)
            root_job = db.execute('SELECT * FROM reseller_owner_interface_jobs').fetchone()
            self.assertEqual((root_job['account_id'], root_job['actor'], root_job['state']), (account['id'], 'mother', 'complete'))
        approval = self.post(f"/resellers/api/accounts/{account['id']}/approval", {'approved': True})
        self.assertEqual(approval.status_code, 200, approval.json)
        self.assertTrue(approval.json['account']['active'])

    def test_owner_only_and_rejects_all_manual_technical_fields(self):
        value = {'username': 'forbidden', 'password': 'a-safe-test-password'}
        self.assertEqual(self.post('/resellers/api/accounts', value).status_code, 401)
        self.client.get('/fixture-owner')
        for field, bad in (('interfaces', [1]), ('iface_id', 1), ('listen_port', 51825),
                           ('endpoint_host', 'other.test'), ('parent_id', 99), ('days', 30)):
            result = self.post('/resellers/api/accounts', {**value, field: bad})
            self.assertEqual(result.status_code, 400, result.json)
        self.assertEqual(self.store.all(), [])
        self.assertEqual(self.native_interface_calls, [])

    def test_admission_errors_create_no_account(self):
        self.client.get('/fixture-owner')
        policy_path = Path(self.app.instance_path) / 'reseller-interface-defaults.json'
        original = policy_path.read_bytes()
        policy_path.unlink()
        value = {'username': 'not-admitted', 'password': 'a-safe-test-password'}
        result = self.post('/resellers/api/accounts', value)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json['error'], 'provision_defaults_required')
        policy_path.write_bytes(original)
        for target, error in (('suggest_port', PortPoolExhausted('test exhausted ports')),
                              ('child_network', NetworkPoolExhausted('test exhausted pool'))):
            with patch('portal_provisioning.' + target, side_effect=error):
                self.assertEqual(self.post('/resellers/api/accounts', value).status_code, 409)
        self.assertEqual(self.store.all(), [])
        self.assertEqual(self.native_interface_calls, [])

    def test_unknown_root_outcome_preserved_and_not_retried_by_create_or_approval(self):
        self.native_interface_error = (502, {'error': 'test unknown startup'})
        first = self.create_owner()
        self.assertEqual(first.status_code, 502, first.json)
        account = first.json['account']
        self.assertEqual(account['interfaces'], [])
        self.assertEqual(account['interface_provision']['state'], 'uncertain')
        self.assertEqual(self.create_owner().status_code, 400)
        approval = f"/resellers/api/accounts/{account['id']}/approval"
        self.assertEqual(self.post(approval, {'approved': True}).status_code, 409)
        self.assertEqual(len(self.native_interface_calls), 1)
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.store.interface_reservations()[0]['port'], 51825)
        recovery = f"/resellers/api/accounts/{account['id']}/interface"
        self.assertEqual(self.post(recovery, {'iface_id': 999}).status_code, 400)
        self.assertEqual(self.store.get(account['id'])['interface_provision']['state'], 'uncertain')
        self.assertEqual(self.post(recovery, {'iface_id': 2}).status_code, 200)
        self.assertFalse(self.store.get(account['id'])['active'])
        self.assertEqual(self.post(approval, {'approved': True}).status_code, 200)
        self.assertEqual(len(self.native_interface_calls), 1)

    def test_legacy_root_without_job_is_safely_recovered_on_approval(self):
        legacy = self.store.create('old-root', 'a-safe-test-password', [], 3, allow_unassigned=True)
        self.client.get('/fixture-owner')
        result = self.post(f"/resellers/api/accounts/{legacy['id']}/approval", {'approved': True})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['account']['active'])
        self.assertEqual(result.json['account']['interface_provision']['kind'], 'owner')
        self.assertEqual(len(self.native_interface_calls), 1)

    def test_owner_child_and_grandchild_receive_distinct_interfaces_from_same_policy(self):
        root = self.create_owner().json['account']
        self.assertEqual(self.post(f"/resellers/api/accounts/{root['id']}/approval", {'approved': True}).status_code, 200)
        self.login()
        child_result = self.post('/reseller/api/accounts', {'username': 'nested-child', 'password': 'a-safe-test-password'})
        self.assertEqual(child_result.status_code, 201, child_result.json)
        child = child_result.json['account']
        self.assertEqual(self.post(f"/resellers/api/accounts/{child['id']}/approval", {'approved': True}).status_code, 200)
        login = self.post('/reseller/api/login', {'username': 'nested-child', 'password': 'a-safe-test-password'})
        self.assertEqual(login.status_code, 200)
        self.csrf = login.json['csrf']
        grand_result = self.post('/reseller/api/accounts', {'username': 'nested-grandchild', 'password': 'a-safe-test-password'})
        self.assertEqual(grand_result.status_code, 201, grand_result.json)
        grand = grand_result.json['account']
        self.assertEqual((child['parent_id'], grand['parent_id']), (root['id'], child['id']))
        self.assertEqual([item['listen_port'] for item in self.native_interface_calls], [51825, 51826, 51827])
        self.assertEqual(len({item['address'] for item in self.native_interface_calls}), 3)
        self.assertEqual(len({root['interfaces'][0], child['interfaces'][0], grand['interfaces'][0]}), 3)
        policy = ProvisionDefaultsStore(self.app.instance_path).required()
        self.assertTrue(all(item['endpoint_host'] == policy['endpoint_host'] for item in self.native_interface_calls))

    def test_cross_root_child_port_reservations_are_transactionally_exclusive(self):
        parent = self.account()
        child = self.store.create('new-child', 'safe-long-password', [], 2, parent_id=parent['id'], allow_unassigned=True)
        root = self.store.create('new-root', 'safe-long-password', [], 3, allow_unassigned=True)
        self.store.reserve_owner_interface(root['id'], 51825, 'mother')
        with self.assertRaises(StoreError):
            self.store.reserve_interface(parent['id'], child['id'], 51825)
        self.store.reserve_interface(parent['id'], child['id'], 51826)
        other = self.store.create('other-root', 'safe-long-password', [], 3, allow_unassigned=True)
        with self.assertRaises(StoreError):
            self.store.reserve_owner_interface(other['id'], 51826, 'mother')
        self.assertEqual({job['port'] for job in self.store.interface_reservations()}, {51825, 51826})

    def test_v5_migration_preserves_credentials_jobs_and_receipts(self):
        parent = self.account()
        child = self.store.create('v5-child', 'safe-long-password', [], 2, parent_id=parent['id'], allow_unassigned=True)
        self.store.reserve_interface(parent['id'], child['id'], 51825)
        self.store.attach_child_interface(parent['id'], child['id'], 2)
        uncertain = self.store.create('v5-uncertain', 'safe-long-password', [], 2, parent_id=parent['id'], allow_unassigned=True)
        self.store.reserve_interface(parent['id'], uncertain['id'], 51826)
        self.store.fail_interface(parent['id'], uncertain['id'], uncertain=True)
        receipt = self.store.reserve(parent['id'], 'v5-created-peer-key', {'name': 'preserve'})
        self.store.finish(receipt['id'], parent['id'], 71, 'M' * 43 + '=')
        self.store.reserve(parent['id'], 'v5-uncertain-peer-key')
        self.store.begin_batch(parent['id'], 'v5-durable-batch', {'count': 2})
        with self.store.db(write=True) as db:
            password_hash = db.execute('SELECT password_hash FROM resellers WHERE id=?', (parent['id'],)).fetchone()[0]
            old_jobs = [dict(row) for row in db.execute('SELECT * FROM reseller_interface_jobs ORDER BY id')]
            old_operations = [dict(row) for row in db.execute('SELECT * FROM reseller_operations ORDER BY id')]
            old_batches = [dict(row) for row in db.execute('SELECT * FROM reseller_batches')]
            # This temporary fixture exactly models the additive difference
            # between old v5 and v6; no production database is touched.
            db.execute('DROP TABLE reseller_owner_interface_jobs')
            db.execute('PRAGMA user_version=5')
        migrated = ResellerStore(self.database)
        self.assertTrue(migrated.get(parent['id'])['active'])
        self.assertEqual(migrated.ownership(parent['id']), {71: 'M' * 43 + '='})
        with migrated.db() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)
            self.assertEqual(db.execute('SELECT password_hash FROM resellers WHERE id=?', (parent['id'],)).fetchone()[0], password_hash)
            self.assertEqual([dict(row) for row in db.execute('SELECT * FROM reseller_interface_jobs ORDER BY id')], old_jobs)
            self.assertEqual([dict(row) for row in db.execute('SELECT * FROM reseller_operations ORDER BY id')], old_operations)
            self.assertEqual([dict(row) for row in db.execute('SELECT * FROM reseller_batches')], old_batches)
            self.assertEqual(db.execute('SELECT count(*) FROM reseller_owner_interface_jobs').fetchone()[0], 0)
        self.assertEqual(migrated.get(uncertain['id'])['interface_provision']['state'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
