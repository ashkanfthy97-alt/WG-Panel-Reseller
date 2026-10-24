import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "setup_state", ROOT / "deploy/linux/setup_state.py"
)
setup_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_state)


class SetupStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = setup_state.SetupState(Path(self.temp.name) / "setup.sqlite3")
        self.token = "K" * 48
        self.state.initialize(self.token, "198.51.100.42")

    def tearDown(self):
        self.temp.cleanup()

    def test_initial_state_never_returns_secret(self):
        status = self.state.read()
        self.assertEqual(status["phase"], "awaiting_owner")
        self.assertEqual(status["public_ip"], "198.51.100.42")
        self.assertNotIn("token_hash", status)
        self.assertNotIn("setup_token", status)

    def test_missing_storage_raises_controlled_error_without_creating_database(self):
        path = Path(self.temp.name) / "absent" / "setup.sqlite3"
        with self.assertRaises(setup_state.SetupStateError):
            setup_state.SetupState(path).read()
        self.assertFalse(path.exists())

    def test_invalid_token_does_not_claim_owner(self):
        with self.assertRaises(setup_state.InvalidSetupToken):
            self.state.begin_owner_claim("wrong", "owner@example.test")
        self.assertFalse(self.state.read()["owner_claimed"])

    def test_owner_claim_is_single_use_and_replay_safe(self):
        claim = self.state.begin_owner_claim(self.token, "owner@example.test")
        self.assertTrue(claim["reserved"])
        self.state.complete_owner_claim(claim["claim_id"])
        self.assertTrue(self.state.read()["owner_claimed"])
        with self.assertRaises(setup_state.SetupAlreadyClaimed):
            self.state.begin_owner_claim(self.token, "another@example.test")

    def test_concurrent_claims_have_exactly_one_reservation(self):
        barrier = threading.Barrier(3)
        outcomes = []
        lock = threading.Lock()

        def claim(username):
            barrier.wait()
            try:
                result = self.state.begin_owner_claim(self.token, username)
                value = ("ok", result["claim_id"])
            except Exception as exc:  # captured for an exact single-winner assertion
                value = ("error", type(exc))
            with lock:
                outcomes.append(value)

        threads = [threading.Thread(target=claim, args=(f"{i}@example.test",)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(item[0] == "ok" for item in outcomes), 1)
        self.assertEqual(sum(item == ("error", setup_state.OwnerClaimInProgress) for item in outcomes), 1)

    def test_reservation_can_be_recovered_without_reopening_owner_claim(self):
        claim = self.state.begin_owner_claim(self.token, "owner@example.test")
        self.state.recover_owner_claim(claim["claim_id"], admin_exists=False)
        status = self.state.read()
        self.assertEqual(status["phase"], "awaiting_owner")
        self.assertTrue(status["token_available"])

    def test_stale_claim_is_released_after_restart(self):
        claim = self.state.begin_owner_claim(self.token, "owner@example.test")
        with self.state._db() as db:
            db.execute("UPDATE setup_state SET updated_at=datetime('now','-5 minutes') WHERE id=1")
        self.assertTrue(self.state.recover_stale_owner_claim(stale_seconds=120))
        self.assertEqual(self.state.read()["phase"], "awaiting_owner")
        self.assertTrue(self.state.read()["token_available"])
        with self.assertRaises(setup_state.InvalidTransition):
            self.state.complete_owner_claim(claim["claim_id"])

    def test_domain_and_completion_require_owner_and_tls(self):
        with self.assertRaises(setup_state.InvalidTransition):
            self.state.set_domain("vpn.example.test", "pending")
        claim = self.state.begin_owner_claim(self.token, "owner@example.test")
        self.state.complete_owner_claim(claim["claim_id"])
        self.state.set_domain("vpn.example.test", "issued")
        with self.assertRaises(setup_state.InvalidTransition):
            self.state.complete_setup(panel_ready=False)
        self.state.mark_tls_ready("wg-portal-domain", "/certs/fullchain.pem")
        self.state.complete_setup(panel_ready=True)
        self.assertEqual(self.state.read()["phase"], "complete")
        with self.assertRaises(setup_state.SetupAlreadyComplete):
            self.state.begin_owner_claim(self.token, "owner@example.test")

    def test_management_can_replace_tls_after_completion_without_reopening_setup(self):
        claim = self.state.begin_owner_claim(self.token, "owner@example.test")
        self.state.complete_owner_claim(claim["claim_id"])
        self.state.mark_tls_ready("wg-portal-bootstrap", "/certs/bootstrap")
        self.state.complete_setup(panel_ready=True)
        snapshot = self.state.read()
        with self.assertRaises(setup_state.InvalidTransition):
            self.state.mark_tls_ready("wg-portal-domain", "/certs/new")
        self.state.mark_tls_ready("wg-portal-domain", "/certs/new", "vpn.example.test", allow_complete=True)
        self.assertEqual(self.state.read()["phase"], "complete")
        self.state.restore_tls_state(snapshot)
        self.assertEqual(self.state.read()["cert_name"], "wg-portal-bootstrap")
        self.assertEqual(self.state.read()["phase"], "complete")
        self.assertFalse(self.state.read()["token_available"])


if __name__ == "__main__":
    unittest.main()
