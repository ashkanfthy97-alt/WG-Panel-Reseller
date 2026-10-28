"""Async scheduling with fake Telegram/state/native operations, never real services."""
import asyncio
from collections import Counter, defaultdict
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from portal_bot_runtime import AsyncBotRuntime
from portal_reseller_bot import BotError, telegram_result


class FakeStore:
    def __init__(self, count):
        self.lock = threading.RLock()
        self.data = {i: dict(reseller_id=i, revision='first', owner_id=1000+i,
                            token_enc=f'fake-token-{i}', offset=0, enabled=True)
                     for i in range(1, count+1)}

    def rows(self):
        with self.lock:
            return [dict(row) for row in self.data.values() if row['enabled']]

    def cipher(self):
        # This test is scheduling-only. Real Fernet coverage lives in test_reseller_bot.
        return SimpleNamespace(decrypt=lambda value: value)

    def progress(self, row, *, offset=None, status=None):
        with self.lock:
            fresh = self.data[row['reseller_id']]
            if fresh['revision'] != row['revision']:
                return
            if offset is not None:
                fresh['offset'] = max(fresh['offset'], offset)
            if status is not None:
                fresh['status'] = status

    def snapshot(self, account_id):
        with self.lock:
            return dict(self.data[account_id])


class FakeManager:
    def __init__(self, count=2):
        self.app = SimpleNamespace(config={})
        self.store = FakeStore(count)
        self.command_started = threading.Event()
        self.release_command = threading.Event()
        self.block_account = None
        self.command_calls = []

    def current(self, row):
        fresh = self.store.snapshot(row['reseller_id'])
        return fresh if fresh['enabled'] and fresh['revision'] == row['revision'] else None

    def account(self, row):
        return {'active': True, 'id': row['reseller_id']} if self.current(row) else None

    def command(self, row, update):
        self.command_calls.append((row['reseller_id'], update['update_id']))
        if row['reseller_id'] == self.block_account:
            self.command_started.set()
            if not self.release_command.wait(timeout=2):
                raise RuntimeError('Test did not release the simulated native command')
        return ('text', f"Account {row['reseller_id']} update {update['update_id']}")

    @staticmethod
    def callback_id(row, update):
        return None

    @staticmethod
    def error_status(error):
        return error.code if isinstance(error, BotError) else 'connection_error'


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.poll_counts = Counter()
        self.updates = defaultdict(list)
        self.poll_failures = {}
        self.delivery_failure = False
        self.holds = defaultdict(asyncio.Event)
        self.closed = False

    async def call(self, token, method, payload=None, **kwargs):
        self.requests.append((token, method, dict(payload or {})))
        if method == 'getUpdates':
            self.poll_counts[token] += 1
            if token in self.poll_failures:
                raise self.poll_failures[token]
            if self.updates[token]:
                return self.updates[token].pop(0)
            await self.holds[token].wait()
            return []
        if self.delivery_failure:
            raise BotError('telegram_connection_failed')
        return True

    async def close(self):
        self.closed = True


class ManyBotsChecks(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, condition):
        async def check():
            while not condition():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(check(), timeout=2)

    async def test_twenty_bots_enter_long_poll_independently(self):
        manager, transport = FakeManager(20), FakeTransport()
        runtime = AsyncBotRuntime(manager, transport, executor_workers=2)
        try:
            await runtime.reconcile_once()
            await self.wait_until(lambda: len(transport.poll_counts) == 20)
            self.assertEqual(len(runtime.tasks), 20)
            self.assertTrue(all(count == 1 for count in transport.poll_counts.values()))
        finally:
            await runtime.close()
        self.assertTrue(transport.closed)
        self.assertFalse(runtime.tasks)

    async def test_rotation_waits_only_for_its_old_native_operation(self):
        manager, transport = FakeManager(3), FakeTransport()
        manager.block_account = 1
        transport.updates['fake-token-1'] = [[{'update_id': 51}]]
        transport.updates['fake-token-2'] = [[]]
        runtime = AsyncBotRuntime(manager, transport, executor_workers=1)
        try:
            await runtime.reconcile_once()
            await self.wait_until(manager.command_started.is_set)
            old_task = runtime.tasks[1][1]
            with manager.store.lock:
                manager.store.data[1].update(revision='second', token_enc='fake-token-1-new')
            await asyncio.wait_for(runtime.reconcile_once(), timeout=0.5)
            self.assertFalse(old_task.done(), 'Old command must retire before replacement starts')
            # A busy native executor must not starve other bots' state checks/polling.
            await self.wait_until(lambda: transport.poll_counts['fake-token-2'] >= 2)
            self.assertTrue(runtime.tasks[2][1] is not old_task)
            manager.release_command.set()
            await self.wait_until(old_task.done)
            await runtime.reconcile_once()
            await self.wait_until(lambda: transport.poll_counts['fake-token-1-new'] == 1)
            self.assertFalse(any(token == 'fake-token-1' and method == 'sendMessage'
                                 for token, method, _ in transport.requests),
                             'A stale credential cannot deliver its completed command')
            self.assertEqual(manager.command_calls, [(1, 51)])
        finally:
            manager.release_command.set()
            await runtime.close()

    async def test_one_rejected_bot_does_not_stop_another_bot(self):
        manager, transport = FakeManager(2), FakeTransport()
        transport.poll_failures['fake-token-1'] = BotError('telegram_invalid_token')
        transport.updates['fake-token-2'] = [[{'update_id': 7}]]
        runtime = AsyncBotRuntime(manager, transport, executor_workers=1)
        try:
            await runtime.reconcile_once()
            await self.wait_until(lambda: manager.store.snapshot(1).get('status') == 'telegram_invalid_token')
            await self.wait_until(lambda: any(method == 'sendMessage' for _, method, _ in transport.requests))
            sent = [(token, payload['chat_id']) for token, method, payload in transport.requests
                    if method == 'sendMessage']
            self.assertEqual(sent, [('fake-token-2', 1002)])
            self.assertEqual(manager.store.snapshot(2)['offset'], 8)
        finally:
            await runtime.close()

    async def test_persisted_offset_survives_delivery_failure_and_runtime_restart(self):
        manager, transport = FakeManager(1), FakeTransport()
        transport.updates['fake-token-1'] = [[{'update_id': 91}]]
        transport.delivery_failure = True
        runtime = AsyncBotRuntime(manager, transport, executor_workers=1)
        try:
            await runtime.reconcile_once()
            await self.wait_until(lambda: manager.store.snapshot(1).get('status') == 'telegram_connection_failed')
            self.assertEqual(manager.store.snapshot(1)['offset'], 92)
        finally:
            await runtime.close()
        fresh_transport = FakeTransport()
        restarted = AsyncBotRuntime(manager, fresh_transport, executor_workers=1)
        try:
            await restarted.reconcile_once()
            await self.wait_until(lambda: fresh_transport.poll_counts['fake-token-1'] == 1)
            request = next(payload for _, method, payload in fresh_transport.requests if method == 'getUpdates')
            self.assertEqual(request['offset'], 92)
            self.assertEqual(manager.command_calls, [(1, 91)], 'Restart must resume beyond the consumed update')
        finally:
            await restarted.close()


class TelegramErrorChecks(unittest.TestCase):
    def test_specific_errors_preserve_only_safe_code_and_retry_delay(self):
        secret = '123456:A_fake_token_that_must_never_be_reported'
        for status, expected, retry in ((401, 'telegram_invalid_token', 0),
                                        (409, 'telegram_conflict', 0),
                                        (429, 'telegram_rate_limited', 17)):
            with self.subTest(status=status), self.assertRaises(BotError) as captured:
                telegram_result(status, {'ok': False, 'error_code': status,
                    'description': 'https://api.telegram.org/bot' + secret + '/getUpdates',
                    'parameters': {'retry_after': retry}})
            self.assertEqual(captured.exception.code, expected)
            self.assertEqual(captured.exception.retry_after, retry)
            self.assertNotIn(secret, str(captured.exception))


if __name__ == '__main__':
    unittest.main()
