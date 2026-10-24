"""Guard control-flow tests; native models/WireGuard are substituted, never run."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from flask import Flask


class GuardChecks(unittest.TestCase):
    def setUp(self):
        self.peer = SimpleNamespace(id=7, public_key='A' * 43 + '=', address='10.7.0.2/24', iface=SimpleNamespace(node_id=None, name='wg0', path='/etc/wireguard/wg0.conf'),
                                    subscription_links=[], used_bytes_total=30, bytes_offset=12, status='offline',
                                    expires_at=9999, limit_bytes=lambda: 100)
        self.query = Mock()
        self.query.filter_by.return_value = self.query
        self.query.with_for_update.return_value = self.query
        self.query.first.return_value = self.peer
        self.db = SimpleNamespace(session=Mock(), engine=SimpleNamespace(dialect=SimpleNamespace(name='sqlite')))
        dependencies = {'auth': SimpleNamespace(require_api_key=lambda fn: fn),
                        'models': SimpleNamespace(Peer=SimpleNamespace(query=self.query), db=self.db),
                        'sqlalchemy': SimpleNamespace(text=lambda value: value)}
        spec = importlib.util.spec_from_file_location('guard_under_test', Path(__file__).resolve().parents[1] / 'deploy/linux/overlays/portal_peer_guard.py')
        self.guard = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, dependencies):
            spec.loader.exec_module(self.guard)
        self.native = {key: Mock() for key in ('_accumulate_peer_usage', '_wg_enable', '_wg_disable', '_sync_peer', '_wg_disable_quiet')}
        self.native.update(_effective_expiry_ts=lambda peer: peer.expires_at, now_ts=lambda: 100)
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.guard.install_peer_guard(self.app, SimpleNamespace(exempt=lambda fn: fn), self.native)
        self.client = self.app.test_client()

    def action(self, action='enable', **extra):
        return self.client.post('/api/portal/owned-peer-action', json=dict(id=7, public_key='A' * 43 + '=', action=action, **extra))

    def test_resume_preserves_consumption_and_expiry(self):
        result = self.action()
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json['data_reset'])
        self.assertEqual((self.peer.used_bytes_total, self.peer.expires_at), (30, 9999))
        self.native['_wg_enable'].assert_called_once_with(self.peer)
        self.db.session.execute.assert_called_once_with('BEGIN IMMEDIATE')
        self.query.filter_by.assert_called_once_with(id=7, public_key='A' * 43 + '=')

    def test_replaced_key_never_reaches_wireguard(self):
        self.query.first.return_value = None
        self.assertEqual(self.action().status_code, 404)
        self.native['_wg_enable'].assert_not_called()

    def test_limit_prevents_resume(self):
        self.peer.used_bytes_total = 100
        self.assertEqual(self.action().status_code, 409)
        self.native['_wg_enable'].assert_not_called()

    def test_expiry_prevents_resume(self):
        self.peer.expires_at = 99
        self.assertEqual(self.action().status_code, 409)

    def test_remote_and_subscription_peers_rejected(self):
        self.peer.iface.node_id = 2
        self.assertEqual(self.action().status_code, 409)
        self.peer.iface.node_id = None
        self.peer.subscription_links = [object()]
        self.assertEqual(self.action().status_code, 409)

    def test_disable_preserves_total_resets_kernel_offset(self):
        self.assertEqual(self.action('disable').status_code, 200)
        self.assertEqual((self.peer.used_bytes_total, self.peer.bytes_offset), (30, 0))

    def test_partial_resume_fails_closed(self):
        self.native['_sync_peer'].side_effect = RuntimeError('config failed')
        with self.assertRaises(RuntimeError):
            self.action()
        saved = self.native['_wg_disable_quiet'].call_args.args[0]
        self.assertIsNot(saved, self.peer)
        self.assertEqual(saved.public_key, 'A' * 43 + '=')
        self.assertEqual(saved.address, '10.7.0.2/24')
        self.db.session.commit.assert_not_called()

    def test_unknown_action_rejected(self):
        self.assertEqual(self.action('reset_data').status_code, 400)


if __name__ == '__main__':
    unittest.main()
