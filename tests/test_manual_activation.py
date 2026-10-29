"""Approval and allocation use only temporary SQLite; no native mutations."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.linux.reseller_store import ResellerStore, StoreError


class ActivationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ResellerStore(Path(self.temp.name) / 'accounts.sqlite3')
        self.parent = self.store.create('parent', 'long-test-password', [1], 10)
        self.store.approve(self.parent['id'])

    def child(self, name='child'):
        return self.store.create(name, 'long-test-password', [], 2,
            parent_id=self.parent['id'], creation_fee_toman=3000000,
            allow_unassigned=True)

    def test_renewal_cannot_replace_manual_approval(self):
        child = self.child()
        value = self.store.update(child['id'], days=30, enabled=True)
        self.assertFalse(value['active'])
        self.assertEqual(value['approval_status'], 'pending')
        account, limited = self.store.authenticate('child', 'long-test-password', '127.0.0.1')
        self.assertFalse(limited)
        self.assertEqual(account['id'], child['id'])
        self.assertNotIn('password_hash', account)

    def test_approval_requires_assignment_and_does_not_extend_on_replay(self):
        child = self.child()
        with self.assertRaises(StoreError):
            self.store.approve(child['id'])
        self.store.reserve_interface(self.parent['id'], child['id'], 51825)
        self.store.attach_child_interface(self.parent['id'], child['id'], 22)
        first = self.store.approve(child['id'], actor='mother')
        self.assertTrue(first['active'])
        self.assertEqual(self.store.approve(child['id'])['expires_at'], first['expires_at'])
        self.store.approve(child['id'], approved=False)
        self.assertFalse(self.store.get(child['id'])['active'])
        self.assertEqual(self.store.approve(child['id'])['expires_at'], first['expires_at'])

    def test_foreign_parent_and_pending_job_retry_are_rejected(self):
        child = self.child()
        other = self.store.create('other', 'long-test-password', [2])
        self.store.approve(other['id'])
        with self.assertRaises(StoreError):
            self.store.reserve_interface(other['id'], child['id'], 51825)
        self.store.reserve_interface(self.parent['id'], child['id'], 51825)
        self.store.fail_interface(self.parent['id'], child['id'], uncertain=True)
        with self.assertRaises(StoreError):
            self.store.reserve_interface(self.parent['id'], child['id'], 51826)
        self.assertEqual(self.store.get(child['id'])['interface_provision']['state'], 'uncertain')

    def test_same_port_concurrent_reservations_have_one_winner(self):
        children = [self.child('child-one'), self.child('child-two')]
        def reserve(child):
            try:
                self.store.reserve_interface(self.parent['id'], child['id'], 51825)
                return True
            except StoreError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sum(executor.map(reserve, children)), 1)

    def test_owner_summary_includes_pending_child_and_ancestor_revocation(self):
        child = self.child()
        self.assertEqual(self.store.summary()['pending_approvals'], 1)
        self.assertEqual(self.store.summary()['total_creation_fee_toman'], 3000000)
        self.store.reserve_interface(self.parent['id'], child['id'], 51825)
        self.store.attach_child_interface(self.parent['id'], child['id'], 22)
        self.store.approve(child['id'])
        self.store.approve(self.parent['id'], approved=False)
        self.assertFalse(self.store.get(child['id'])['active'])
        self.assertEqual(self.store.get(child['id'])['interfaces'], [22])


if __name__ == '__main__':
    unittest.main()
