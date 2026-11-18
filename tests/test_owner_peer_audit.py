"""Temporary SQLite and fake native API; no server or Telegram calls."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = Path(os.environ.get('OWNER_AUDIT_SUPPORT_ROOT', ROOT))
sys.path.insert(0, str(SUPPORT))
sys.path.insert(0, str(ROOT / 'deploy/linux'))
spec = importlib.util.spec_from_file_location('owner_audit_routes', ROOT / 'deploy/linux/portal_reseller.py')
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)


class OwnerPeerAuditChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = routes.ResellerStore(Path(self.temp.name) / 'resellers.sqlite3')
        self.parent = self.store.create('parent', 'safe-test-password', [1], 200)
        self.store.approve(self.parent['id'])
        self.child = self.store.create('child', 'safe-test-password', [1], 100, parent_id=self.parent['id'])
        self.store.approve(self.child['id'])
        self.grand = self.store.create('grandchild', 'safe-test-password', [1], 100, parent_id=self.child['id'])
        self.store.approve(self.grand['id'])
        self.native = []
        self.native_error = None
        outer = self
        class Core:
            def json(self, method, path, payload=None, *, cookie=None):
                if path == '/api/admin':
                    return (200, {'username':'mother'}) if cookie == 'session=owner' else (401,{})
                if path == '/api/get-interfaces':
                    return 200, {'interfaces':[{'id':i,'name':'wg'+str(i)} for i in range(1,4)]}
                if path == '/api/peers':
                    return outer.native_error or (200, {'peers':outer.native})
                raise AssertionError(path)
        self.app = Flask(__name__, instance_path=self.temp.name)
        self.app.config.update(TESTING=True,SECRET_KEY='x'*40,SESSION_COOKIE_NAME='wgportal_reseller_session')
        self.app.register_blueprint(routes.reseller_blueprint('http://unused',self.store.path,'native-secret',core=Core()))
        self.client = self.app.test_client()
        self.client.set_cookie('session','owner')

    def peer(self, account, pid, key=None):
        key = key or ('K'+str(pid))
        with self.store.db(write=True) as db:
            db.execute('INSERT INTO reseller_peers VALUES(?,?,?,?)',(pid,account['id'],key,1))
        self.native.append({'id':pid,'public_key':key,'private_key':'never-show',
            'shortlink_token':'never-show','api_key':'never-show','name':'customer '+str(pid),
            'iface_id':account['interfaces'][0],'address':'10.0.0.2/32',
            'used_bytes':1024,'ttl_seconds':60,'status':'online'})

    def test_all_accounts_and_grandchildren_have_complete_counts_and_details(self):
        for pid in range(1,106): self.peer(self.parent,pid)
        self.peer(self.child,201)
        self.peer(self.grand,301)
        result=self.client.get('/resellers/api/peers')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['total'],107)
        counts={row['reseller_id']:row['peer_count'] for row in result.json['counts']}
        self.assertEqual(counts,{self.parent['id']:105,self.child['id']:1,self.grand['id']:1})
        listing=self.client.get('/resellers/api/accounts').json['accounts']
        self.assertEqual({row['id']:row['peer_count'] for row in listing},counts)
        self.assertEqual(result.json['peers'][0]['address'],'10.0.0.2/32')
        for key in ('public_key','private_key','api_key','shortlink_token','never-show'):
            self.assertNotIn(key,result.get_data(as_text=True))

    def test_filter_is_direct_owner_and_archived_account_keeps_current_peer_visibility(self):
        self.peer(self.parent,1);self.peer(self.child,2);self.peer(self.grand,3)
        self.store.archive(self.child['id'])
        result=self.client.get('/resellers/api/peers?reseller_id='+str(self.child['id']))
        self.assertEqual(result.json['total'],1)
        self.assertEqual(result.json['peers'][0]['id'],2)
        self.assertTrue(result.json['peers'][0]['reseller']['deleted'])
        self.assertEqual(self.client.get('/resellers/api/peers?reseller_id=0').status_code,400)
        self.assertEqual(self.client.get('/resellers/api/peers?reseller_id=999').status_code,404)

    def test_deleted_and_reused_native_ids_are_never_attributed_to_previous_owner(self):
        self.peer(self.parent,1);self.peer(self.parent,2)
        self.native.pop()
        self.native[0]['public_key']='replacement-key'
        result=self.client.get('/resellers/api/peers')
        self.assertEqual(result.json['total'],0)
        self.assertTrue(all(row['peer_count']==0 for row in result.json['counts']))

    def test_only_native_owner_can_read_audit(self):
        self.client.delete_cookie('session')
        for endpoint in ('/resellers/api/peers','/resellers/api/accounts'):
            self.assertEqual(self.client.get(endpoint,headers={'X-API-KEY':'native-secret'}).status_code,401)
        self.client.set_cookie('wgpanel_reseller_session','forged-reseller')
        self.assertEqual(self.client.get('/resellers/api/peers').status_code,401)
        state=self.client.get('/reseller/api/session').json
        login=self.client.post('/reseller/api/login',json={'username':'parent','password':'safe-test-password'},
            headers={'Origin':'https://localhost','X-Reseller-CSRF':state['csrf']})
        self.assertEqual(login.status_code,200,login.json)
        self.assertEqual(self.client.get('/resellers/api/peers').status_code,401)
        self.assertEqual(self.client.get('/resellers/api/accounts').status_code,401)

    def test_unavailable_or_partial_inventory_never_looks_like_successful_zero(self):
        for value in ((503,{}),(200,{'error':'peer_query_failed','peers':[]}),(200,{'peers':[],'total':9}),(200,{'peers':[{}]})):
            self.native_error=value
            self.assertEqual(self.client.get('/resellers/api/peers').status_code,503)
            self.assertEqual(self.client.get('/resellers/api/accounts').status_code,503)
        self.native_error=None
        with patch.object(routes.ResellerStore,'peer_owners',side_effect=sqlite3.OperationalError('mock index down')):
            self.assertEqual(self.client.get('/resellers/api/peers').status_code,503)


if __name__=='__main__': unittest.main()
