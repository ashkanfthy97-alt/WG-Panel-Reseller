"""Staged Telegram navigation and privileged bridge, with no network or real users."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import threading
import tempfile
from types import SimpleNamespace
import unittest
from flask import Blueprint, Flask, g, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = Path(os.environ['OPERATIONS_SUPPORT_ROOT'])
sys.path.insert(0, str(SUPPORT))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'deploy/linux'))
import portal_native_reseller_admin_bot as admin
import portal_reseller_bot as dealer
from portal_reseller_admin import install_admin_bridge


class AdministratorMenus(unittest.TestCase):
    def setUp(self):
        self.context=SimpleNamespace(user_data={})
        self.update=SimpleNamespace(effective_user=SimpleNamespace(id=123,username='mother'),effective_message=SimpleNamespace(text=''))
        self.sent=[];self.calls=[]
    async def send(self, update, text, keyboard): self.sent.append((text,keyboard))
    def api(self, method, path, **kw):
        body=kw['payload'];self.calls.append(body)
        self.assertEqual((method,path),('POST','/api/portal/reseller-admin'))
        self.assertEqual(body['actor']['id'],'123')
        action=body['action']
        if action=='list': return {'accounts':[]}
        if action=='peers': return {'peers':[{'id':i+80,'name':'<customer>'} for i in range(7)]}
        if action=='notices_list': return {'notices':[{'id':i+1,'title':'<notice>','active':True} for i in range(10)]}
        if action.startswith('notices_'): return {'notice':{'id':9}}
        return {'account':{'id':4,'username':'dealer','peer_count':7,'max_peers':20}}
    def callback(self, value): return asyncio.run(admin.handle_callback(self.update,self.context,value,self.api,self.send))
    def text(self, value):
        self.update.effective_message.text=value
        return asyncio.run(admin.handle_text(self.update,self.context,self.api,self.send))
    def test_announcement_preview_confirmation_escape_and_duplicate_button(self):
        self.callback('resellers:noticecompose');self.text('<title>');self.text('<'*2000)
        self.assertEqual(self.calls,[], 'Preview must not publish')
        self.assertTrue(any('&lt;title&gt;' in msg for msg,_ in self.sent))
        self.assertTrue(all(len(msg.encode('utf-16-le'))//2<4096 for msg,_ in self.sent))
        nonce=self.context.user_data[admin.STATE]['request_id']
        self.callback('resellers:confirm:'+nonce)
        self.assertEqual(self.calls[-1]['action'],'notices_create')
        self.assertEqual(self.calls[-1]['payload'],{'title':'<title>','body':'<'*2000})
        self.callback('resellers:confirm:'+nonce)
        self.assertEqual(len(self.calls),1)
    def test_archive_and_reactivate_need_confirmation(self):
        for action,active in [('noticearchive',False),('noticeactivate',True)]:
            self.callback('resellers:'+action+':9');before=len(self.calls)
            nonce=self.context.user_data[admin.STATE]['request_id']
            self.callback('resellers:confirm:'+nonce)
            self.assertEqual(len(self.calls),before+1)
            self.assertEqual(self.calls[-1]['payload'],{'id':9,'active':active})
    def test_history_is_paged_and_peer_rows_numbered(self):
        self.callback('resellers:notices')
        cards=[text for text,_ in self.sent if '&lt;notice&gt;' in text]
        self.assertEqual(len(cards),8)
        self.assertIn('resellers:notices:1',str(self.sent[-1][1]))
        self.sent.clear();self.callback('resellers:peers:4')
        self.assertIn('7',self.sent[0][0]);self.assertIn('1. &lt;customer&gt;',self.sent[1][0])
        self.assertIn('7. &lt;customer&gt;',self.sent[2][0])
        self.sent.clear();self.callback('resellers:open:4');self.assertIn('7 / 20',self.sent[-1][0])
    def test_transport_failure_retains_receipt_without_exception_detail(self):
        self.callback('resellers:noticearchive:9');nonce=self.context.user_data[admin.STATE]['request_id']
        def failed(*a,**kw):raise RuntimeError('secret transport detail')
        asyncio.run(admin.handle_callback(self.update,self.context,'resellers:confirm:'+nonce,failed,self.send))
        self.assertEqual(self.context.user_data[admin.STATE]['request_id'],nonce)
        self.assertNotIn('secret transport detail',str(self.sent))


class DealerMenus(unittest.TestCase):
    def setUp(self):
        self.manager=dealer.BotManager.__new__(dealer.BotManager)
        self.manager.command_lock=threading.RLock();self.manager.prompt_lock=threading.Lock()
        self.manager.command_locks={};self.manager.wizards={};self.manager.completed_updates={};self.manager.prompts={}
        self.manager.current=lambda row:row
        self.manager.account=lambda row:{'id':row['reseller_id'],'active':True}
        self.row={'reseller_id':1,'revision':'fixture','owner_id':123,'bot_id':999}
        self.calls=[]
        self.manager.dispatch=self.dispatch
    def dispatch(self,row,method,path,**kw):
        self.calls.append((method,path,kw))
        if path=='/reseller/api/peers':return 200,{'peers':[{'id':80+i,'name':'customer'+str(i)} for i in range(12)]},b''
        if path.endswith('/config'):return 200,None,b'[Interface]\nPrivateKey=fixture\n'
        if path.endswith('/qr'):return 200,None,b'\x89PNGfixture'
        if path.endswith('/user-link'):return 200,{'url':'https://example.test/u/scoped'},b''
        if path=='/reseller/api/accounts':return 201,{'account':{'id':2,'creation_fee_toman':5000000}},b''
        raise AssertionError((method,path))
    def update(self,text='',*,owner=123,kind='private',uid=101,data=None):
        msg={'text':text,'from':{'id':owner},'chat':{'id':owner,'type':kind}}
        return {'update_id':uid,'message':msg} if data is None else {'update_id':uid,'callback_query':{'id':'q','from':{'id':owner},'message':msg,'data':data}}
    def test_reply_menu_native_four_actions_and_old_inline_navigation(self):
        result=self.manager.command(self.row,self.update('/start'))
        markup=result[1][0]['reply_markup']
        self.assertEqual([b['text'] for line in markup['keyboard'] for b in line],['➕ ساخت PEER','👥 ساخت نماینده','📦 ساخت انبوه','⚙️ تنظیمات'])
        self.assertTrue(markup['resize_keyboard'])
        result=self.manager.command(self.row,self.update(data='reseller:settings',uid=102))
        self.assertIn('کانفیگ',str(result[1][0]['reply_markup']))
    def test_native_cards_keep_file_qr_caption_link_and_controls(self):
        kind,items=self.manager.command(self.row,self.update(data='peer:bundle:80'))
        self.assertEqual(kind,'delivery');self.assertEqual([i['method'] for i in items],['sendDocument','sendPhoto'])
        self.assertIn('PrivateKey=fixture',items[0]['caption']);self.assertIn('/u/scoped',items[1]['caption'])
        self.assertEqual([b['callback_data'] for line in items[1]['reply_markup']['inline_keyboard'] for b in line],['peer:bundle:80','peer:edit:80','peers:menu'])
    def test_numbering_across_pages_and_scope_gate(self):
        result=self.manager.command(self.row,self.update('/peers 1'))
        self.assertEqual(result[1][0]['text'],'📋 تعداد مشتری‌ها: 12')
        self.assertTrue(result[1][1]['text'].startswith('11. '));self.assertTrue(result[1][2]['text'].startswith('12. '))
        for msg in [self.update('/start',owner=9),self.update('/start',kind='group')]:
            self.assertIsNone(self.manager.command(self.row,msg))
    def test_child_wizard_keeps_password_private_and_replay_safe(self):
        self.manager.command(self.row,self.update('👥 ساخت نماینده'))
        self.manager.command(self.row,self.update('child',uid=102))
        update=self.update('long-enough-password',uid=103)
        first=self.manager.command(self.row,update);second=self.manager.command(self.row,update)
        self.assertEqual(first,second)
        self.assertEqual(len(self.calls),1)
        self.assertNotIn('long-enough-password',str(first)+str(self.manager.wizards)+str(self.manager.completed_updates))


class PrivilegedBridge(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__);self.app.config.update(TESTING=True,API_KEY='fixture-secret')
        @self.app.get('/api/telegram/admins')
        def registry():return jsonify(admins=[{'id':'123'}])
        self.observed=[]
        def owner_handler():
            self.observed.append((g.reseller_owner,request.args.to_dict(),request.get_json(silent=True),request.headers.get('Idempotency-Key')))
            return jsonify(peers=[],notice={'id':9},notices=[],csrf='must-strip')
        def wrapped():raise AssertionError('must use validated service body')
        wrapped.__wrapped__=owner_handler
        def edit_handler(notice_id):
            self.assertEqual(notice_id,9);return owner_handler()
        def edit_wrapped():raise AssertionError('not used')
        edit_wrapped.__wrapped__=edit_handler
        bp=Blueprint('testbridge',__name__)
        install_admin_bridge(bp,None,{'peers':(wrapped,'/resellers/api/peers','GET'),'notices_create':(wrapped,'/resellers/api/notices','POST'),'notices_update':(edit_wrapped,'/resellers/api/notices/{id}','PUT')},None)
        self.app.register_blueprint(bp);self.client=self.app.test_client()
    def bridge(self,action,payload,actor='123',key=True):
        return self.client.post('/api/portal/reseller-admin',json={'action':action,'actor':{'id':actor},'payload':payload,'request_id':'mock-operation-key-1234'},headers={'X-API-KEY':'fixture-secret'} if key else {})
    def test_two_gates_and_scoped_peer_query(self):
        self.assertEqual(self.bridge('peers',{'id':4},key=False).status_code,401)
        self.assertEqual(self.bridge('peers',{'id':4},actor='999').status_code,403)
        good=self.bridge('peers',{'id':4})
        self.assertEqual(good.status_code,200);self.assertNotIn('csrf',good.json)
        self.assertEqual(self.observed[-1][0:2],('telegram-admin:123',{'reseller_id':'4'}))
        self.assertEqual(self.bridge('peers',{'id':True}).status_code,400)
    def test_notice_id_and_receipt_are_forwarded(self):
        result=self.bridge('notices_update',{'id':9,'active':False})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.observed[-1][2],{'active':False})
        self.assertEqual(self.bridge('notices_create',{'title':'test','body':'body'}).status_code,200)
        self.assertEqual(self.observed[-1][3],'mock-operation-key-1234')


class AssumedOwnerBotSettings(unittest.TestCase):
    def test_native_owner_must_still_be_valid_on_bot_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            app=Flask(__name__);app.config.update(TESTING=True,SECRET_KEY='temporary',RESELLER_BOT_CSRF_FACTORY=lambda:'test-csrf')
            dealer.install_reseller_bot(app,None,Path(directory)/'resellers.sqlite3')
            manager=app.extensions['reseller_bots']
            account={'id':1,'active':True,'auth_version':3}
            manager.accounts.get=lambda _:account
            manager.store=SimpleNamespace(get=lambda _:None,public=lambda _: {'configured':False})
            client=app.test_client()
            with client.session_transaction() as s:
                s['reseller_identity']={'id':1,'version':3};s['assumed_owner']='mother'
            self.assertEqual(client.get('/reseller/api/bot').status_code,401,'No guard must fail closed')
            app.extensions['portal_reseller_identity_guard']=lambda:None
            self.assertEqual(client.get('/reseller/api/bot').status_code,401)
            app.extensions['portal_reseller_identity_guard']=lambda:account
            self.assertEqual(client.get('/reseller/api/bot').status_code,200)
            with client.session_transaction() as s:s.pop('assumed_owner')
            app.extensions['portal_reseller_identity_guard']=lambda:None
            self.assertEqual(client.get('/reseller/api/bot').status_code,200,'Normal reseller auth stays isolated')


if __name__=='__main__':unittest.main()
