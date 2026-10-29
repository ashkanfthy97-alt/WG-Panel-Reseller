"""One async poll task per reseller bot; bounded threads for native operations.

The caller holds the process leader lock. Rotating one credential only retires
that task; the other bots keep polling while its native operation finishes.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import inspect
import json

from portal_reseller_bot import (BotError, TELEGRAM_METHODS, protect_transport_logs,
                                 telegram_payload, telegram_result)


class AsyncTelegramTransport:
    def __init__(self):
        import httpx
        protect_transport_logs()
        self.httpx = httpx
        self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=100))

    async def call(self, token, method, payload=None, *, document=None, photo=None):
        if method not in TELEGRAM_METHODS:
            raise BotError('unsupported_telegram_operation')
        payload = dict(payload or {})
        filename = payload.pop('filename', 'wireguard.conf')
        files = ({'document': (filename, document, 'text/plain')}
                 if document is not None else {'photo': ('wireguard.png', photo, 'image/png')}
                 if photo is not None else None)
        try:
            timeout = self.httpx.Timeout(40 if method == 'getUpdates' else 15, connect=5, write=15, pool=15)
            async with self.client.stream('POST', 'https://api.telegram.org/bot' + token + '/' + method,
                    data=telegram_payload(payload), files=files, timeout=timeout) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        raise BotError('telegram_response_invalid')
                return telegram_result(response.status_code, json.loads(data))
        except BotError:
            raise
        except self.httpx.HTTPError:
            raise BotError('telegram_connection_failed') from None
        except (ValueError, TypeError, UnicodeError):
            raise BotError('telegram_response_invalid') from None

    async def close(self):
        await self.client.aclose()


class AsyncBotRuntime:
    def __init__(self, manager, transport=None, *, executor_workers=8, reconcile_interval=3):
        self.manager = manager
        self.transport = transport or manager.app.config.get('RESELLER_BOT_ASYNC_TRANSPORT')
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='reseller-bot-state')
        self.native_executor = ThreadPoolExecutor(max_workers=max(1, min(64, int(executor_workers))),
                                                  thread_name_prefix='reseller-native')
        self.reconcile_interval = reconcile_interval
        self.tasks = {}  # reseller_id -> (credential snapshot, asyncio.Task)

    async def blocking(self, function, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(self.executor,
                                                               partial(function, *args, **kwargs))

    async def native_command(self, row, update):
        return await asyncio.get_running_loop().run_in_executor(self.native_executor,
                                         partial(self.manager.command, row, update))

    async def send_answer(self, row, token, answer):
        kind, value = answer
        if not await self.blocking(self.manager.current, row) or not await self.blocking(self.manager.account, row):
            return
        if kind == 'delivery':
            for item in value:
                if not await self.blocking(self.manager.current, row) or not await self.blocking(self.manager.account, row):
                    return
                payload = dict(item)
                method = payload.pop('method')
                files = {key: payload.pop(key) for key in ('document', 'photo') if key in payload}
                await self.transport.call(token, method, {'chat_id': row['owner_id'], **payload}, **files)
            return
        if kind in {'document', 'photo'}:
            await self.transport.call(token, 'sendDocument' if kind == 'document' else 'sendPhoto',
                {'chat_id': row['owner_id']}, **{kind: value})
            return
        for item in value if kind == 'messages' else [{'text': value}]:
            if not await self.blocking(self.manager.current, row) or not await self.blocking(self.manager.account, row):
                return
            await self.transport.call(token, 'sendMessage', {'chat_id': row['owner_id'],
                                      'disable_web_page_preview': 'true', **item})

    async def poll(self, row):
        try:
            token = await self.blocking(lambda: self.manager.store.cipher().decrypt(row['token_enc'].encode()).decode())
        except Exception:
            await self.blocking(self.manager.store.progress, row, status='credential_error')
            return
        inflight = None
        try:
            while True:
                fresh = await self.blocking(self.manager.current, row)
                if not fresh:
                    return
                if not await self.blocking(self.manager.account, row):
                    await self.blocking(self.manager.store.progress, row, status='panel_paused')
                    await asyncio.sleep(5)
                    continue
                try:
                    updates = await self.transport.call(token, 'getUpdates', {'offset': fresh['offset'],
                        'timeout': 25, 'limit': 20, 'allowed_updates': '["message","callback_query"]'})
                    if not isinstance(updates, list):
                        raise BotError('telegram_response_invalid')
                    await self.blocking(self.manager.store.progress, row, status='running')
                    for update in updates:
                        if not await self.blocking(self.manager.current, row):
                            return
                        if (not isinstance(update, dict) or type(update.get('update_id')) is not int
                                or update['update_id'] < fresh['offset']):
                            continue
                        inflight = asyncio.create_task(self.native_command(row, update))
                        answer = await asyncio.shield(inflight)
                        inflight = None
                        # Persist before delivery. Create/renew receipts also
                        # protect an interrupted native operation from replay.
                        await self.blocking(self.manager.store.progress, row, offset=update['update_id'] + 1)
                        fresh['offset'] = update['update_id'] + 1
                        if answer and await self.blocking(self.manager.current, row) and await self.blocking(self.manager.account, row):
                            callback_id = self.manager.callback_id(row, update)
                            if callback_id:
                                await self.transport.call(token, 'answerCallbackQuery', {'callback_query_id': callback_id})
                            await self.send_answer(row, token, answer)
                    # Telegram long polls usually wait; a fake/empty immediate
                    # reply must not create a busy loop in tests or on errors.
                    if not updates:
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    await self.blocking(self.manager.store.progress, row, status=self.manager.error_status(error))
                    await asyncio.sleep(max(3, getattr(error, 'retry_after', 0)))
        finally:
            # Do not start a replacement poller until the old native operation
            # has finished. Waiting here never stalls other bots' async tasks.
            if inflight is not None:
                try:
                    await asyncio.shield(inflight)
                except Exception:
                    pass

    async def reconcile_once(self):
        rows = {row['reseller_id']: row for row in await self.blocking(self.manager.store.rows)}
        for account_id, (row, task) in list(self.tasks.items()):
            if task.done():
                if not task.cancelled():
                    task.exception()  # consume a failed task without logging secrets
                self.tasks.pop(account_id)
            elif account_id not in rows or row['revision'] != rows[account_id]['revision']:
                # Cancellation is issued once: a second cancellation could
                # interrupt the retirement wait for an in-flight native call.
                if not getattr(task, '_portal_retiring', False):
                    task._portal_retiring = True
                    task.cancel()
        for account_id, row in rows.items():
            if account_id not in self.tasks:
                self.tasks[account_id] = (row, asyncio.create_task(self.poll(row),
                    name='reseller-bot-' + str(account_id)))

    async def close(self):
        tasks = [task for _, task in self.tasks.values()]
        for task in tasks:
            if not getattr(task, '_portal_retiring', False):
                task._portal_retiring = True
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.executor.shutdown(wait=True)
        self.native_executor.shutdown(wait=True)
        close = getattr(self.transport, 'close', None)
        if close:
            value = close()
            if inspect.isawaitable(value):
                await value

    async def run(self, stop=None):
        stop = stop or asyncio.Event()
        if self.transport is None:
            self.transport = AsyncTelegramTransport()
        try:
            while not stop.is_set():
                await self.reconcile_once()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.reconcile_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.close()
