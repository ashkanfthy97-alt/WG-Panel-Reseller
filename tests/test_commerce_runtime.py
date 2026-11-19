"""Exercise the commerce runtime workers without Telegram or WireGuard."""
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import unittest

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portal_commerce import _run_runtime_leader


class CommerceRuntimeTest(unittest.TestCase):
    def test_polling_stays_responsive_while_provisioning_and_delivery_wait(self):
        stop = threading.Event()
        release = threading.Event()
        provisioning_started = threading.Event()
        delivery_started = threading.Event()
        polling_continued = threading.Event()
        poll_flags = []

        def process_jobs():
            provisioning_started.set()
            release.wait(8)

        def notify_pending(*, limit):
            delivery_started.set()
            release.wait(8)

        def poll_once(*, deliver_notifications=True):
            poll_flags.append(deliver_notifications)
            if provisioning_started.wait(4) and delivery_started.wait(4):
                if len(poll_flags) >= 2:
                    polling_continued.set()
                    stop.set()

        app = Flask(__name__)
        config = SimpleNamespace(
            row=lambda: {'enabled': True, 'token_enc': 'fake', 'base_url': 'https://panel.test'},
            token=lambda row: 'fixture',
            ensure_isolated=lambda token: None,
            status=lambda state: None,
        )
        service = SimpleNamespace(
            process_jobs=process_jobs, refresh_subscriptions=lambda: None, expire_payments=lambda: None,
        )
        bot = SimpleNamespace(poll_once=poll_once, notify_pending=notify_pending)
        supervisor = threading.Thread(
            target=_run_runtime_leader, args=(app, service, config, lambda row, token: bot, stop),
            daemon=True,
        )
        supervisor.start()
        try:
            self.assertTrue(polling_continued.wait(5))
            self.assertEqual(poll_flags, [False, False])
            self.assertTrue(supervisor.is_alive(), 'Leadership is kept while worker tasks drain')
        finally:
            release.set()
            stop.set()
            supervisor.join(4)
        self.assertFalse(supervisor.is_alive())


if __name__ == '__main__':
    unittest.main()
