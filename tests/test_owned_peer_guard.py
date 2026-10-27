"""Guard route with fake ORM/kernel and real temporary SQLite receipt transactions."""
import ast
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from flask import Flask, abort, jsonify, request

ROOT = Path(__file__).resolve().parents[1]


class GuardChecks(unittest.TestCase):
    def setUp(self):
        connection = sqlite3.connect(':memory:', isolation_level=None)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        class Result:
            def __init__(self, cursor): self.cursor = cursor
            def mappings(self): return self
            def first(self):
                value = self.cursor.fetchone()
                return dict(value) if value else None
        class Session:
            def execute(self, query, parameters=None): return Result(connection.execute(query, parameters or {}))
            def rollback(self): connection.rollback()
            def commit(self): connection.commit()
        class Engine:
            dialect = SimpleNamespace(name='sqlite')
            @contextmanager
            def begin(self):
                try:
                    yield Session()
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        self.peer = SimpleNamespace(id=1, public_key='A'*43+'=', private_key='unchanged-key',
            address='10.0.0.2/32', iface=SimpleNamespace(name='wg0', path='/unused/wg0', node_id=None),
            subscription_links=[], name='customer', unlimited=False, start_on_first_use=False,
            data_limit_value=10, data_limit_unit='Gi', time_limit_days=10,
            used_bytes_total=3*1024**3, status='offline', endpoint='vpn.test:51820', mtu=1380)
        self.peer.limit_bytes = lambda: self.peer.data_limit_value * (1024**2 if self.peer.data_limit_unit == 'Mi' else 1024**3)
        peer = self.peer
        class Query:
            def filter_by(self, **values): self.values=values;return self
            def with_for_update(self): return self
            def first(self): return peer if all(getattr(peer,key)==value for key,value in self.values.items()) else None
        namespace = dict(abort=abort,jsonify=jsonify,request=request,text=lambda value:value,
            SimpleNamespace=SimpleNamespace,hashlib=hashlib,re=re,
            require_api_key=lambda function:function,
            Peer=SimpleNamespace(query=Query()),db=SimpleNamespace(session=Session(),engine=Engine()))
        tree = ast.parse((ROOT/'portal_peer_guard.py').read_text(encoding='utf-8'))
        functions = [value for value in tree.body if isinstance(value,ast.FunctionDef)]
        exec(compile(ast.Module(body=functions,type_ignores=[]),'guard.py','exec'),namespace)
        self.app = Flask(__name__)
        self.app.config['TESTING']=True
        self.native = {name:Mock() for name in ('_accumulate_peer_usage','_sync_peer','_wg_enable','_wg_disable','_wg_disable_quiet','_sync_effective_expiry')}
        self.native.update(now_ts=lambda:86400,_effective_expiry_ts=lambda peer:10*86400,
            _timer_anchor_ts=lambda peer:0,from_ts=lambda value:value)
        namespace['install_peer_guard'](self.app,SimpleNamespace(exempt=lambda value:value),self.native)
        self.client=self.app.test_client()

    def post(self, **changes):
        body=dict(id=1,public_key=self.peer.public_key,action='renew',days=5,volume_gb=7,request_id='guard-request-00000001')
        body.update(changes)
        return self.client.post('/api/portal/owned-peer-action',json=body)

    def test_receipt_replay_does_not_add_another_package(self):
        self.assertEqual(self.post().status_code,200)
        quota=self.peer.limit_bytes()
        self.assertEqual(quota,17*1024**3)
        second=self.post()
        self.assertEqual(second.status_code,200)
        self.assertTrue(second.json['replay'])
        self.assertEqual(self.peer.limit_bytes(),quota)
        self.assertEqual(self.peer.used_bytes_total,3*1024**3)
        self.assertEqual(self.peer.private_key,'unchanged-key')
        self.assertEqual(self.peer.endpoint,'vpn.test:51820')
        self.assertEqual(self.peer.mtu,1380)
        self.native['_accumulate_peer_usage'].assert_called_once()
        self.native['_wg_enable'].assert_not_called()  # Manual offline state stays offline.

    def test_conflicting_receipt_is_rejected(self):
        self.assertEqual(self.post().status_code,200)
        self.assertEqual(self.post(days=6).status_code,409)
        self.assertEqual(self.peer.limit_bytes(),17*1024**3)

    def test_public_key_and_technical_field_forgery_rejected(self):
        self.assertEqual(self.post(public_key='B'*43+'=').status_code,404)
        self.assertEqual(self.post(endpoint='evil.test:42').status_code,400)
        self.assertEqual(self.peer.limit_bytes(),10*1024**3)


if __name__=='__main__':
    unittest.main()
