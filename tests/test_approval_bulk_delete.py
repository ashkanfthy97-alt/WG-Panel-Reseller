"""Temporary SQLite + fake native/WireGuard checks; no VPS or Telegram access."""
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
import sqlite3
import unittest
from unittest.mock import Mock

import test_native_reseller as native_fixture
import test_reseller as facade_fixture
import test_owned_peer_guard as guard_fixture
from deploy.linux.reseller_store import ResellerStore, StoreError


class ApprovalArchiveChecks(unittest.TestCase):
    setUp = native_fixture.NativeResellerChecks.setUp
    post = native_fixture.NativeResellerChecks.post
    login = native_fixture.NativeResellerChecks.login
    account = native_fixture.NativeResellerChecks.account

    def test_v4_upgrade_preserves_credentials_ownership_and_operation_receipts(self):
        path = Path(self.temporary.name) / 'old-v4.sqlite3'
        parent = self.account()
        with self.store.db() as source:
            password_hash = source.execute('SELECT password_hash FROM resellers WHERE id=?', (parent['id'],)).fetchone()[0]
        with closing(sqlite3.connect(path)) as old:
            old.executescript('''CREATE TABLE resellers (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                expires_at INTEGER NOT NULL DEFAULT 0, max_peers INTEGER NOT NULL DEFAULT 100,
                interfaces TEXT NOT NULL DEFAULT '[]', auth_version INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL, parent_id INTEGER REFERENCES resellers(id),
                creation_fee_toman INTEGER NOT NULL DEFAULT 0,
                approval_status TEXT NOT NULL DEFAULT 'approved', approved_at INTEGER,
                approved_by TEXT DEFAULT 'legacy-migration');
                CREATE TABLE reseller_peers (peer_id INTEGER PRIMARY KEY,
                    reseller_id INTEGER NOT NULL REFERENCES resellers(id),
                    public_key TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
                CREATE TABLE reseller_operations (id INTEGER PRIMARY KEY,
                    reseller_id INTEGER NOT NULL REFERENCES resellers(id), request_id TEXT NOT NULL,
                    state TEXT NOT NULL, peer_id INTEGER, created_at INTEGER NOT NULL,
                    UNIQUE(reseller_id,request_id));
                PRAGMA user_version=4;''')
            old.execute('''INSERT INTO resellers
                (id,username,password_hash,expires_at,max_peers,interfaces,auth_version,created_at,approval_status,approved_at)
                VALUES(7,'v4-account',?,4102444800,10,'[1]',9,1700000000,'approved',1700000000)''', (password_hash,))
            old.execute('INSERT INTO reseller_peers VALUES(77,7,?,1700000000)', ('V' * 43 + '=',))
            old.execute("INSERT INTO reseller_operations VALUES(4,7,'old-complete-operation','complete',77,1700000000)")
            old.execute("INSERT INTO reseller_operations VALUES(5,7,'old-uncertain-operation','uncertain',NULL,1700000000)")
            old.commit()
        migrated = ResellerStore(path)
        self.assertTrue(migrated.get(7)['active'])
        self.assertFalse(migrated.get(7)['deleted'])
        self.assertEqual(migrated.get(7)['auth_version'], 9)
        self.assertEqual(migrated.ownership(7), {77: 'V' * 43 + '='})
        receipt = migrated.reserve(7, 'old-complete-operation')
        self.assertEqual((receipt['state'], receipt['peer_id'], receipt['payload_hash']), ('complete', 77, None))
        self.assertEqual(migrated.reserve(7, 'old-uncertain-operation')['state'], 'uncertain')
        self.assertEqual(migrated.credit(7)['reserved_peers'], 1)
        with migrated.db() as upgraded:
            self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0], 7)
            self.assertEqual(upgraded.execute('SELECT password_hash FROM resellers WHERE id=7').fetchone()[0], password_hash)
            self.assertEqual(upgraded.execute('SELECT count(*) FROM reseller_operations').fetchone()[0], 2)

    def delete(self, path, value=None, *, headers=None):
        return self.client.delete(path, json={'confirm': True} if value is None else value,
            headers=headers or {'Origin': 'http://localhost', 'X-Reseller-CSRF': self.csrf})

    def test_sequential_owner_approvals_are_independent_and_idempotent(self):
        self.client.get('/fixture-owner')
        accounts = [self.store.create('dealer-' + str(i), 'safe-long-password', [1], 3) for i in range(3)]
        for account in accounts:
            path = f"/resellers/api/accounts/{account['id']}/approval"
            result = self.post(path, {'approved': True, 'days': 30})
            self.assertEqual(result.status_code, 200, result.json)
            self.assertTrue(result.json['account']['active'])
            expiry = result.json['account']['expires_at']
            self.assertEqual(self.post(path, {'approved': True, 'days': 30}).json['account']['expires_at'], expiry)
        self.assertTrue(all(self.store.get(a['id'])['active'] for a in accounts))

    def test_legacy_unassigned_children_recovered_separately(self):
        parent = self.account()
        children = [self.store.create('old-child-' + str(i), 'safe-long-password', [], 2,
            parent_id=parent['id'], allow_unassigned=True) for i in range(2)]
        self.client.get('/fixture-owner')
        assigned, ports = [], []
        for child in children:
            result = self.post(f"/resellers/api/accounts/{child['id']}/approval", {'approved': True})
            self.assertEqual(result.status_code, 200, result.json)
            self.assertTrue(result.json['account']['active'])
            assigned.extend(result.json['account']['interfaces'])
            ports.append(self.native_interface_calls[-1]['listen_port'])
        self.assertEqual(len(set(assigned)), 2)
        self.assertEqual(ports, [51825, 51826])

    def test_uncertain_job_needs_explicit_owner_assignment_without_activation(self):
        parent = self.account()
        child = self.store.create('uncertain-child', 'safe-long-password', [], 2,
            parent_id=parent['id'], allow_unassigned=True)
        self.store.reserve_interface(parent['id'], child['id'], 51825)
        self.store.fail_interface(parent['id'], child['id'], uncertain=True)
        self.client.get('/fixture-owner')
        approval = f"/resellers/api/accounts/{child['id']}/approval"
        self.assertEqual(self.post(approval, {'approved': True}).status_code, 409)
        self.assertEqual(self.native_interface_calls, [])
        assignment = f"/resellers/api/accounts/{child['id']}/interface"
        self.assertEqual(self.post(assignment, {'iface_id': 999}).status_code, 400)
        self.assertEqual(self.store.get(child['id'])['interface_provision']['state'], 'uncertain')
        self.assertEqual(self.post(assignment, {'iface_id': 2}).status_code, 200)
        recovered = self.store.get(child['id'])
        self.assertEqual(recovered['interface_provision']['state'], 'complete')
        self.assertFalse(recovered['active'])
        self.assertEqual(recovered['approval_status'], 'pending')
        self.assertEqual(self.post(approval, {'approved': True}).status_code, 200)

    def test_archive_revokes_login_keeps_peers_children_and_disables_bot(self):
        parent = self.account()
        child = self.store.create('kept-child', 'safe-long-password', [1], 2, parent_id=parent['id'])
        self.store.approve(child['id'])
        operation = self.store.reserve(parent['id'], 'preserved-peer-receipt')
        self.store.finish(operation['id'], parent['id'], 91, 'K' * 43 + '=')
        old_version = parent['auth_version']
        disable = Mock()
        self.app.extensions['reseller_bots'] = SimpleNamespace(store=SimpleNamespace(disable=disable))
        self.login()
        self.client.get('/fixture-owner')
        path = f"/resellers/api/accounts/{parent['id']}"
        result = self.delete(path)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['resources_preserved'])
        self.assertGreater(result.json['account']['auth_version'], old_version)
        self.assertTrue(result.json['account']['deleted'])
        self.assertEqual(self.client.get('/reseller/api/peers').status_code, 401)
        self.assertFalse(self.store.get(child['id'])['active'])
        self.assertIn(91, self.store.ownership(parent['id']))
        self.assertIsNotNone(self.store.get(child['id']))
        authenticated, limited = self.store.authenticate('dealer', 'a-safe-test-password', 'temporary')
        self.assertIsNone(authenticated)
        self.assertFalse(limited)
        disable.assert_called_once_with(parent['id'])
        self.assertEqual(self.delete(path).status_code, 200)
        self.assertEqual(self.store.get(parent['id'])['auth_version'], old_version + 1)

    def test_parent_delete_only_direct_child_and_explicit_csrf_confirmation(self):
        parent = self.account()
        child = self.store.create('mine', 'safe-long-password', [1], 2, parent_id=parent['id'])
        unrelated = self.store.create('not-mine', 'safe-long-password', [2], 2)
        self.login()
        path = f"/reseller/api/accounts/{child['id']}"
        self.assertEqual(self.delete(path, headers={'Origin': 'http://localhost'}).status_code, 403)
        self.assertEqual(self.delete(path, {'confirm': 1}).status_code, 400)
        self.assertEqual(self.delete(f"/reseller/api/accounts/{unrelated['id']}").status_code, 404)
        self.assertEqual(self.delete(path).status_code, 200)
        self.assertFalse(self.store.get(unrelated['id'])['deleted'])


class BulkDeleteChecks(unittest.TestCase):
    setUp = facade_fixture.ResellerChecks.setUp
    tearDown = facade_fixture.ResellerChecks.tearDown
    post = facade_fixture.ResellerChecks.post
    login = facade_fixture.ResellerChecks.login
    create = facade_fixture.ResellerChecks.create

    def bulk(self, *, key='bulk-request-000000001', **changes):
        value = {'name_prefix': 'customer', 'count': 2, 'days': 30, 'volume_gb': 5}
        value.update(changes)
        return self.post('/reseller/api/peers/bulk', value, key)

    def delete(self, peer_id, **changes):
        value = {'confirm': True}
        value.update(changes)
        return self.client.delete(f'/reseller/api/peers/{peer_id}', json=value,
            headers={'Origin': 'https://localhost', 'X-Reseller-CSRF': self.csrf})

    def test_bulk_quota_partial_replay_and_payload_conflict(self):
        self.login()
        first = self.bulk(count=3)
        self.assertEqual(first.status_code, 207, first.json)
        self.assertTrue(first.json['complete'])
        self.assertFalse(first.json['uncertain'])
        self.assertEqual([p['name'] for p in first.json['created']], ['customer-1', 'customer-2'])
        self.assertEqual(first.json['failed'][0]['index'], 3)
        calls = len([c for c in self.core.calls if c[:2] == ('POST', '/api/peers')])
        replay = self.bulk(count=3)
        self.assertTrue(replay.json['replay'])
        self.assertEqual(len(self.core.rows), 2)
        self.assertEqual(len([c for c in self.core.calls if c[:2] == ('POST', '/api/peers')]), calls)
        self.assertEqual(self.bulk(count=3, days=31).status_code, 400)
        self.assertEqual(self.store.credit(self.a['id'])['remaining_peers'], 0)

    def test_bulk_success_uses_assigned_technical_defaults(self):
        self.login()
        response = self.bulk()
        self.assertEqual(response.status_code, 201, response.json)
        self.assertTrue(response.json['ok'])
        payloads = [c[2] for c in self.core.calls if c[:2] == ('POST', '/api/peers')]
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertEqual(payload['iface_id'], 1)
            self.assertEqual(payload['endpoint'], 'vpn.example.test:51820')
            self.assertEqual(payload['mtu'], 1380)
            self.assertFalse(payload['unlimited'])

    def test_uncertain_batch_stops_and_never_reissues(self):
        self.login()
        self.core.uncertain = True
        first = self.bulk()
        self.assertEqual(first.status_code, 409)
        self.assertTrue(first.json['uncertain'])
        self.assertFalse(first.json['complete'])
        self.assertEqual(len(first.json['failed']), 1)
        self.core.uncertain = False
        self.assertEqual(self.bulk().status_code, 409)
        self.assertEqual(len([c for c in self.core.calls if c[:2] == ('POST', '/api/peers')]), 1)
        self.assertEqual(self.store.credit(self.a['id'])['reserved_peers'], 1)

    def test_bulk_admission_rejects_roles_limits_and_missing_idempotency(self):
        self.login()
        for changes in ({'count': 0}, {'count': 21}, {'count': True}, {'name_prefix': 'x' * 64},
                        {'endpoint': 'evil.test:1'}, {'iface_id': 2}, {'parent_id': 10}):
            with self.subTest(changes=changes):
                self.assertEqual(self.bulk(**changes).status_code, 400)
        self.assertEqual(self.bulk(key='').status_code, 400)
        self.assertEqual(self.core.rows, [])

    def test_peer_delete_failure_preserves_quota_then_success_forgets_and_blocks_create_replay(self):
        self.login()
        peer_id = self.create().json['id']
        original = self.core.json
        failure = {'enabled': True}
        def guarded(method, path, payload=None, **kwargs):
            if path == '/api/portal/owned-peer-action':
                self.assertEqual(payload['public_key'], self.core.rows[0]['public_key'])
                self.assertEqual(payload['iface_id'], 1)
                return (502, {'error': 'runtime'}) if failure['enabled'] else (200, {'success': True})
            return original(method, path, payload, **kwargs)
        self.core.json = guarded
        self.assertEqual(self.delete(peer_id, confirm=1).status_code, 400)
        self.assertEqual(self.delete(peer_id).status_code, 409)
        self.assertIn(peer_id, self.store.ownership(self.a['id']))
        failure['enabled'] = False
        self.assertEqual(self.delete(peer_id).status_code, 200)
        self.assertNotIn(peer_id, self.store.ownership(self.a['id']))
        self.assertEqual(self.store.credit(self.a['id'])['remaining_peers'], 2)
        self.assertTrue(self.delete(peer_id).json['replay'])
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.delete(999).status_code, 404)


class NativeDeleteGuardChecks(unittest.TestCase):
    def setUp(self):
        guard_fixture.GuardChecks.setUp(self)
        self.peer.iface_id = 1
        self.native['_remove_local_peer_runtime_and_config'] = Mock()
        self.native['_delete_peer_rows'] = Mock()

    def delete(self, **changes):
        value = {'id': 1, 'public_key': 'A' * 43 + '=', 'action': 'delete', 'iface_id': 1}
        value.update(changes)
        return self.client.post('/api/portal/owned-peer-action', json=value)

    def test_delete_reuses_native_runtime_then_rows(self):
        events = []
        self.native['_remove_local_peer_runtime_and_config'].side_effect = lambda p: events.append('runtime-config')
        self.native['_delete_peer_rows'].side_effect = lambda p: events.append('rows')
        self.assertEqual(self.delete().status_code, 200)
        self.assertEqual(events, ['runtime-config', 'rows'])

    def test_native_failure_does_not_delete_rows(self):
        self.native['_remove_local_peer_runtime_and_config'].side_effect = RuntimeError('simulated kernel failure')
        with self.assertRaises(RuntimeError):
            self.delete()
        self.native['_delete_peer_rows'].assert_not_called()

    def test_reused_id_absent_old_key_cannot_delete_new_occupant(self):
        self.peer.public_key = 'B' * 43 + '='
        result = self.delete()
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['absent'])
        self.native['_remove_local_peer_runtime_and_config'].assert_not_called()
        self.native['_delete_peer_rows'].assert_not_called()

    def test_interface_move_and_subscribed_peer_rejected(self):
        self.assertEqual(self.delete(iface_id=2).status_code, 409)
        self.peer.subscription_links = [object()]
        self.assertEqual(self.delete().status_code, 409)
        self.native['_delete_peer_rows'].assert_not_called()

    def test_matching_key_under_different_id_is_not_claimed_absent(self):
        self.peer.id = 2
        self.assertEqual(self.delete().status_code, 404)
        self.native['_delete_peer_rows'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
