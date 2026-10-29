"""Native-owner audit index using temporary accounts and fake native routes."""
import sqlite3
import unittest
from unittest.mock import patch

import test_native_reseller as fixture


class ConnectionOwnerChecks(unittest.TestCase):
    setUp = fixture.NativeResellerChecks.setUp
    post = fixture.NativeResellerChecks.post
    account = fixture.NativeResellerChecks.account
    login = fixture.NativeResellerChecks.login

    def own(self, account, peer_id, key):
        operation = self.store.reserve(account['id'], 'owner-fixture-peer-' + str(peer_id))
        self.store.finish(operation['id'], account['id'], peer_id, key)

    def test_requires_native_owner_not_anonymous_reseller_or_api_key(self):
        path = '/resellers/api/peer-owners'
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={'X-API-KEY': self.app.config['API_KEY']}).status_code, 401)
        self.account()
        self.login()
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={'X-API-KEY': self.app.config['API_KEY']}).status_code, 401)
        self.client.get('/fixture-owner')
        result = self.client.get(path)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json, {'owners': []})
        owner_checks = [item for item in self.native_calls if item[0] == '/api/admin']
        self.assertTrue(owner_checks)
        self.assertTrue(all(not key for _, _, key in owner_checks))

    def test_parent_child_archived_ownership_keeps_exact_public_key_identity(self):
        parent = self.account()
        child = self.store.create('child-owner', 'safe-child-password', [1], 2, parent_id=parent['id'])
        self.store.approve(child['id'])
        parent_key, child_key = 'P' * 43 + '=', 'C' * 43 + '='
        self.own(parent, 81, parent_key)
        self.own(child, 82, child_key)
        self.store.archive(child['id'])
        self.client.get('/fixture-owner')
        response = self.client.get('/resellers/api/peer-owners')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'owners': [
            {'peer_id': 81, 'public_key': parent_key,
             'reseller': {'id': parent['id'], 'username': 'dealer', 'deleted': False, 'parent_id': None}},
            {'peer_id': 82, 'public_key': child_key,
             'reseller': {'id': child['id'], 'username': 'child-owner', 'deleted': True, 'parent_id': parent['id']}},
        ]})
        text = response.get_data(as_text=True)
        for secret_field in ('password_hash', 'private_key', 'token', 'auth_version', 'expires_at'):
            self.assertNotIn(secret_field, text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        # There is no invented administrator record for untracked/reused IDs.
        self.assertNotIn('mother', text)

    def test_mapping_failure_is_explicit_and_never_successful_empty_index(self):
        self.client.get('/fixture-owner')
        with patch('deploy.linux.reseller_store.ResellerStore.peer_owners', side_effect=sqlite3.OperationalError('simulated unavailable audit database')):
            result = self.client.get('/resellers/api/peer-owners')
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json, {'error': 'owner_mapping_unavailable'})
        self.assertNotIn('owners', result.json)
        self.assertNotIn('database', result.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
