"""Credit reporting with temporary SQLite and simulated native application routes."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.linux.reseller_store import ResellerStore, StoreError
import test_native_reseller as fixture


class CreditStoreChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = patch('deploy.linux.reseller_store.time.time', return_value=1800000000)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.store = ResellerStore(Path(self.temporary.name) / 'resellers.sqlite3')
        self.parent = self.store.create('parent', 'long-test-password', [1], 5)
        self.store.approve(self.parent['id'], days=10)

    def test_quota_counts_owned_records_and_pending_uncertain_reservations(self):
        account_id = self.parent['id']
        completed = self.store.reserve(account_id, 'completed-operation-0001')
        self.store.finish(completed['id'], account_id, 101, 'owned-key-one')
        failed = self.store.reserve(account_id, 'failed-operation-0001')
        self.store.failed(failed['id'])
        uncertain = self.store.reserve(account_id, 'uncertain-operation-0001')
        self.store.failed(uncertain['id'], uncertain=True)
        self.store.reserve(account_id, 'pending-operation-0001')
        other = self.store.create('other', 'long-test-password', [2], 5)
        self.store.approve(other['id'])
        foreign = self.store.reserve(other['id'], 'foreign-operation-0001')
        self.store.finish(foreign['id'], other['id'], 102, 'foreign-key')

        credit = self.store.credit(account_id)
        self.assertEqual(credit['max_peers'], 5)
        self.assertEqual(credit['used_peers'], 1)
        self.assertEqual(credit['reserved_peers'], 2)
        self.assertEqual(credit['remaining_peers'], 2)
        # Complete operations must not count both their receipt and ownership;
        # failed operations release capacity while uncertain outcomes retain it.
        self.store.reserve(account_id, 'pending-operation-0002')
        self.store.reserve(account_id, 'pending-operation-0003')
        self.assertEqual(self.store.credit(account_id)['remaining_peers'], 0)
        with self.assertRaises(StoreError):
            self.store.reserve(account_id, 'capacity-overflow-0001')

    def test_ancestor_expiry_and_renewal_change_effective_remaining_time(self):
        child = self.store.create('child', 'long-test-password', [1], 3,
                                  parent_id=self.parent['id'])
        self.store.approve(child['id'], days=20)
        first = self.store.credit(child['id'])
        self.assertEqual(first['remaining_seconds'], 10 * 86400)
        self.assertEqual(first['own_remaining_seconds'], 20 * 86400)
        self.assertEqual(first['server_time'], 1800000000)
        self.store.update(self.parent['id'], days=5)
        self.assertEqual(self.store.credit(child['id'])['remaining_seconds'], 15 * 86400)
        with self.store.db(write=True) as db:
            db.execute('UPDATE resellers SET expires_at=? WHERE id=?',
                       (1799999999, self.parent['id']))
        expired = self.store.credit(child['id'])
        self.assertEqual(expired['remaining_seconds'], 0)
        self.assertFalse(expired['active'])
        self.assertEqual(expired['own_remaining_seconds'], 20 * 86400)

    def test_pending_and_disabled_accounts_report_credit_without_granting_access(self):
        pending = self.store.create('pending', 'long-test-password', [2], 4)
        value = self.store.credit(pending['id'])
        self.assertEqual(value['remaining_seconds'], 0)
        self.assertEqual(value['remaining_peers'], 4)
        self.assertEqual(value['approval_status'], 'pending')
        self.assertFalse(value['active'])
        self.store.update(self.parent['id'], enabled=False)
        value = self.store.credit(self.parent['id'])
        self.assertFalse(value['active'])
        self.assertFalse(value['enabled'])
        self.assertEqual(value['remaining_seconds'], 10 * 86400)
        with self.assertRaises(StoreError):
            self.store.reserve(self.parent['id'], 'paused-operation-0001')

    def test_unknown_or_stale_identity_has_no_credit(self):
        self.assertIsNone(self.store.credit(99999))
        self.assertIsNone(self.store.credit(True))
        self.assertIsNone(self.store.credit(self.parent['id'], auth_version=99999))
        current = self.store.get(self.parent['id'])
        self.assertIsNotNone(self.store.credit(current['id'], auth_version=current['auth_version']))


class CreditNativeChecks(unittest.TestCase):
    setUp = fixture.NativeResellerChecks.setUp
    post = fixture.NativeResellerChecks.post
    account = fixture.NativeResellerChecks.account
    login = fixture.NativeResellerChecks.login

    def test_credit_is_own_account_only_and_does_not_call_native_api(self):
        account = self.account()
        self.store.create('foreign', 'long-test-password', [2], 999)
        operation = self.store.reserve(account['id'], 'owned-record-operation-0001')
        self.store.finish(operation['id'], account['id'], 500, 'record-without-live-native-peer')
        self.assertEqual(self.rows, [])
        self.login()
        before = list(self.native_calls)
        # Supplying someone else's ID cannot select the credit account.
        result = self.client.get('/reseller/api/credit?reseller_id=2')
        self.assertEqual(result.status_code, 200)
        credit = result.json['credit']
        self.assertEqual(credit['max_peers'], 3)
        self.assertEqual(credit['used_peers'], 1)
        self.assertEqual(credit['remaining_peers'], 2)
        self.assertEqual(self.native_calls, before)
        self.assertNotIn('password', result.get_data(as_text=True))
        self.assertEqual(self.client.get('/reseller/api/credit/2').status_code, 404)

    def test_pending_or_paused_session_can_read_but_cannot_create(self):
        pending = self.store.create('dealer', 'a-safe-test-password', [1], 3)
        self.login()
        result = self.client.get('/reseller/api/credit')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json['credit']['active'])
        self.assertEqual(result.json['credit']['approval_status'], 'pending')
        self.assertEqual(self.post('/reseller/api/peers', {'name':'blocked','days':30,'volume_gb':10}).status_code, 403)
        self.store.approve(pending['id'])
        self.store.update(pending['id'], enabled=False)
        result = self.client.get('/reseller/api/credit')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json['credit']['active'])
        self.assertFalse(result.json['credit']['enabled'])

    def test_unauthenticated_native_owner_and_stale_reseller_session_are_denied(self):
        self.assertEqual(self.client.get('/reseller/api/credit').status_code, 401)
        self.client.get('/fixture-owner')
        self.assertEqual(self.client.get('/reseller/api/credit').status_code, 401)
        account = self.account()
        self.login()
        self.store.assign_interface(account['id'], 2)
        self.assertEqual(self.client.get('/reseller/api/credit').status_code, 401)


if __name__ == '__main__':
    unittest.main()
