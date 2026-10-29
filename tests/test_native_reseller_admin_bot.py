"""Native administrator-bot reseller navigation with simulated Telegram/API only."""
import ast
from pathlib import Path
import unittest
from types import SimpleNamespace
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portal_native_reseller_admin_bot import handle_callback, handle_text, STATE


class NativeAdminBotChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context=SimpleNamespace(user_data={})
        self.update=SimpleNamespace(effective_user=SimpleNamespace(id=123,username='mother'),effective_message=SimpleNamespace(text=''))
        self.calls=[];self.sent=[]
        self.account={'id':1,'username':'<owner-customer>','parent_id':None,'active':False,'enabled':True,'approval_status':'pending','interfaces':[3],'max_peers':10,'creation_fee_toman':5000000}
        self.defaults={'endpoint_host':'vpn.example.test','dns':'1.1.1.1','mtu':1380,'allowed_ips':'0.0.0.0/0, ::/0','persistent_keepalive':25,'network_pool':'10.0.0.0/8','subnet_prefix':24,'child_max_peers':100,'auto_firewall':True}
    async def send(self,update,message,keyboard):self.sent.append((message,keyboard))
    def api(self,method,path,**kwargs):
        body=kwargs['payload'];self.calls.append(body)
        self.assertEqual((method,path),('POST','/api/portal/reseller-admin'))
        self.assertEqual(body['actor']['id'],'123')
        action=body['action']
        if action=='list':return {'accounts':[self.account]}
        if action in {'defaults_get','defaults_set'}:return {'defaults':dict(self.defaults),'configured':True}
        if action=='create':return {'account':{'id':2,'username':body['payload']['username']}}
        return {'ok':True,'account':dict(self.account)}
    async def callback(self,data):return await handle_callback(self.update,self.context,data,self.api,self.send)
    async def text(self,value):
        self.update.effective_message.text=value
        return await handle_text(self.update,self.context,self.api,self.send)
    async def test_list_detail_and_navigation_escape_names(self):
        self.assertTrue(await self.callback('resellers:menu'))
        buttons=[b for line in self.sent[-1][1]['inline_keyboard'] for b in line]
        self.assertTrue(any(b['callback_data']=='resellers:create' for b in buttons))
        self.assertTrue(any(b['callback_data']=='resellers:defaults' for b in buttons))
        await self.callback('resellers:open:1')
        self.assertIn('&lt;owner-customer&gt;',self.sent[-1][0])
        self.assertNotIn('<owner-customer>',self.sent[-1][0])
    async def test_archive_requires_confirmation_and_stale_button_cannot_repeat(self):
        await self.callback('resellers:archive:1')
        self.assertEqual(self.calls,[])
        nonce=self.context.user_data[STATE]['request_id']
        await self.callback('resellers:confirm:'+nonce)
        self.assertEqual(self.calls[-1]['payload'],{'id':1,'confirm':True})
        self.assertEqual(self.calls[-1]['action'],'archive')
        await self.callback('resellers:confirm:'+nonce)
        self.assertEqual(len(self.calls),1)
    async def test_renewal_retry_keeps_same_request_identity(self):
        await self.callback('resellers:renew:1');await self.text('۳۰')
        nonce=self.context.user_data[STATE]['request_id'];original=self.api
        attempts=[]
        def uncertain(method,path,**kwargs):
            attempts.append(kwargs['payload']['request_id'])
            if len(attempts)==1:return {'error':'connection_error','detail':'do not expose this secret'}
            return original(method,path,**kwargs)
        await handle_callback(self.update,self.context,'resellers:confirm:'+nonce,uncertain,self.send)
        self.assertNotIn('do not expose',str(self.sent))
        await handle_callback(self.update,self.context,'resellers:confirm:'+nonce,uncertain,self.send)
        self.assertEqual(attempts,[nonce,nonce])
        self.assertEqual(self.calls[-1]['payload'],{'id':1,'days':30})
    async def test_create_two_credentials_and_quota_never_echoes_password(self):
        await self.callback('resellers:create')
        await self.text('dealer');await self.text('private-long-password');await self.text('50')
        self.assertEqual(self.calls[-1]['action'],'create')
        self.assertEqual(self.calls[-1]['payload'],{'username':'dealer','password':'private-long-password','max_peers':50})
        self.assertNotIn('private-long-password',str(self.sent))
        self.assertNotIn('private-long-password',str(self.context.user_data))
    async def test_html_or_missing_operation_result_never_claims_success(self):
        await self.callback('resellers:archive:1');nonce=self.context.user_data[STATE]['request_id']
        def html_response(*args,**kwargs):return {'ok':True,'result':'<html>login</html>'}
        await handle_callback(self.update,self.context,'resellers:confirm:'+nonce,html_response,self.send)
        self.assertIn(STATE,self.context.user_data)
        self.assertNotIn('✅ عملیات ثبت شد.',self.sent[-1][0])

    async def test_cancel_expiry_and_switching_native_tab_clear_flow(self):
        await self.callback('resellers:create');await self.text('dealer')
        await self.text('/cancel');self.assertNotIn(STATE,self.context.user_data)
        await self.callback('resellers:create');self.context.user_data[STATE]['expires']=0
        await self.text('expired');self.assertNotIn(STATE,self.context.user_data)
        await self.callback('resellers:create')
        self.assertFalse(await self.callback('home:main'))
        self.assertNotIn(STATE,self.context.user_data)
        self.assertEqual(self.calls,[])
    async def test_defaults_edit_reuses_complete_policy_and_requires_confirmation(self):
        await self.callback('resellers:defaults');await self.callback('resellers:field:mtu');await self.text('۱۴۲۰')
        self.assertFalse(any(c['action']=='defaults_set' for c in self.calls))
        nonce=self.context.user_data[STATE]['request_id'];await self.callback('resellers:confirm:'+nonce)
        self.assertEqual(self.calls[-1]['action'],'defaults_set')
        self.assertEqual(self.calls[-1]['payload'],dict(self.defaults,mtu=1420))
        self.assertNotIn('listen_port',self.calls[-1]['payload'])
    def test_native_handlers_keep_private_administrator_gate(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'telegram_bot.py').read_text(encoding='utf-8'))
        for name,hook in (('on_cb','reseller_admin_callback'),('on_text','reseller_admin_text')):
            node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            self.assertTrue(any(isinstance(d,ast.Name) and d.id=='admin_only' for d in node.decorator_list))
            self.assertTrue(any(isinstance(c,ast.Call) and isinstance(c.func,ast.Name) and c.func.id==hook for c in ast.walk(node)))


if __name__=='__main__':unittest.main()
