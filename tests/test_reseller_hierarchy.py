"""Temporary hierarchy/migration/API checks; no real service or database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.linux.reseller_store import ResellerStore, StoreError
import test_native_reseller as native_fixture


class HierarchyStoreChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'resellers.sqlite3'
        self.store = ResellerStore(self.path)
        self.parent = self.store.create('parent', 'safe-password-long', [1], 10)
        self.store.update(self.parent['id'], days=30)

    def child(self, username='child', parent=None):
        return self.store.create(username, 'safe-password-long', [1], 3,
            parent_id=parent or self.parent['id'], creation_fee_toman=3000000)

    def test_child_pending_until_mother_renewal(self):
        child = self.child()
        self.assertFalse(child['active'])
        self.assertEqual(child['parent_id'], self.parent['id'])
        self.assertEqual(child['creation_fee_toman'], 3000000)
        self.assertTrue(self.store.update(child['id'], days=30)['active'])

    def test_recursive_counts_and_fees_do_not_double_count(self):
        child = self.child('child-one')
        self.child('child-two')
        self.store.update(child['id'], days=30)
        self.child('grandchild', child['id'])
        root = self.store.get(self.parent['id'])
        self.assertEqual(root['direct_child_count'], 2)
        self.assertEqual(root['descendant_count'], 3)
        self.assertEqual(len(self.store.children(root['id'])), 2)
        self.assertEqual(self.store.summary(), dict(total_resellers=4, subresellers=3, total_creation_fee_toman=9000000))

    def test_ancestor_pause_expiry_renewal_preserve_child(self):
        child = self.child()
        self.store.update(child['id'], days=30)
        self.store.update(self.parent['id'], enabled=False)
        self.assertFalse(self.store.get(child['id'])['active'])
        with self.assertRaises(StoreError):
            self.store.reserve(child['id'], 'blocked-operation-0001')
        self.store.update(self.parent['id'], enabled=True)
        self.assertTrue(self.store.get(child['id'])['active'])
        with self.store.db(write=True) as db:
            db.execute('UPDATE resellers SET expires_at=1 WHERE id=?', (self.parent['id'],))
        self.assertFalse(self.store.get(child['id'])['active'])
        self.store.update(self.parent['id'], days=30)
        self.assertTrue(self.store.get(child['id'])['active'])
        self.assertEqual(len(self.store.children(self.parent['id'])), 1)

    def test_parent_limits_are_transactionally_enforced(self):
        for interfaces, limit in (([2], 2), ([1], 11)):
            with self.assertRaises(StoreError):
                self.store.create('forbidden', 'safe-password-long', interfaces, limit,
                                  parent_id=self.parent['id'], creation_fee_toman=3000000)
        self.store.update(self.parent['id'], enabled=False)
        with self.assertRaises(StoreError):
            self.child()
        self.assertEqual(len(self.store.all()), 1)

    def legacy(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''CREATE TABLE resellers (
              id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
              expires_at INTEGER NOT NULL DEFAULT 0, max_peers INTEGER NOT NULL DEFAULT 100,
              interfaces TEXT NOT NULL DEFAULT '[]', auth_version INTEGER NOT NULL DEFAULT 1,
              created_at INTEGER NOT NULL);
              INSERT INTO resellers VALUES(7,'legacy','hash-preserved',1,4102444800,50,'[1]',3,1700000000);
              PRAGMA user_version=2;''')
        return path

    def test_v2_migration_preserves_account_and_hash(self):
        path = self.legacy()
        account = ResellerStore(path).get(7)
        self.assertIsNone(account['parent_id'])
        self.assertEqual(account['creation_fee_toman'], 0)
        self.assertTrue(account['active'])
        self.assertEqual(account['auth_version'], 3)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT password_hash FROM resellers WHERE id=7').fetchone()[0], 'hash-preserved')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_concurrent_v2_migration(self):
        path = self.legacy()
        with ThreadPoolExecutor(max_workers=4) as executor:
            accounts = list(executor.map(lambda _: ResellerStore(path).get(7), range(8)))
        self.assertTrue(all(value['username'] == 'legacy' for value in accounts))
        self.assertEqual(len(ResellerStore(path).all()), 1)


class HierarchyNativeChecks(unittest.TestCase):
    setUp = native_fixture.NativeResellerChecks.setUp
    post = native_fixture.NativeResellerChecks.post
    login = native_fixture.NativeResellerChecks.login
    account = native_fixture.NativeResellerChecks.account

    def test_paused_valid_account_can_sign_in(self):
        account = self.account()
        self.store.update(account['id'], enabled=False)
        result = self.login()
        self.assertFalse(result.json['account']['active'])
        self.assertEqual(self.client.get('/reseller/api/peers').status_code, 403)
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code, 401)

    def test_child_creation_and_owner_summary(self):
        parent = self.account()
        self.login()
        result = self.post('/reseller/api/accounts', dict(username='child', password='safe-password-long', interfaces=[1], max_peers=2))
        self.assertEqual(result.status_code, 201, result.get_data(as_text=True))
        child = result.json['account']
        self.assertEqual(result.json['signin_url'], '/reseller')
        self.assertEqual(child['parent_id'], parent['id'])
        self.assertEqual(child['creation_fee_toman'], 3000000)
        self.assertFalse(child['active'])
        result = self.client.get('/reseller/api/accounts')
        self.assertEqual(len(result.json['accounts']), 1)
        self.assertEqual(result.json['creation_fee']['amount'], 3000000)
        self.assertNotIn('password_hash', result.get_data(as_text=True))
        self.assertEqual(self.client.get('/resellers/api/summary').status_code, 401)
        self.client.get('/fixture-owner')
        result = self.client.get('/resellers/api/summary')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['summary']['subresellers'], 1)
        self.assertEqual(result.json['summary']['total_creation_fee_toman'], 3000000)

    def test_parent_cannot_forge_ownership_price_or_activation(self):
        self.account()
        self.login()
        payload = dict(username='forged', password='safe-password-long', interfaces=[1], max_peers=2)
        for field, value in (('parent_id', 999), ('creation_fee_toman', 0), ('enabled', True), ('days', 365)):
            result = self.post('/reseller/api/accounts', dict(payload, **{field: value}))
            self.assertIn(result.status_code, (400, 403))
        self.assertIn(self.post('/reseller/api/accounts', dict(payload, interfaces=[2])).status_code, (400, 403))
        self.assertEqual(self.post('/resellers/api/accounts/1/renew', {'days': 365}).status_code, 401)

    def test_direct_children_are_private_to_parent(self):
        parent = self.account()
        other = self.store.create('unrelated', 'safe-password-long', [1], 3)
        self.store.update(other['id'], days=30)
        self.store.create('unrelated-child', 'safe-password-long', [1], 2, parent_id=other['id'], creation_fee_toman=3000000)
        self.store.create('my-child', 'safe-password-long', [1], 2, parent_id=parent['id'], creation_fee_toman=3000000)
        self.login()
        result = self.client.get('/reseller/api/accounts')
        self.assertEqual([value['username'] for value in result.json['accounts']], ['my-child'])


if __name__ == '__main__':
    unittest.main()
