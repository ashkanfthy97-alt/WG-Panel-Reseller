"""Real scoped reseller routing, encrypted test tokens, mocked native/Telegram IO."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import test_reseller_bot as fixture
from portal_reseller_native import NativeApplicationCore
from portal_reseller_bot import TelegramTransport, BotError


class MenuChecks(unittest.TestCase):
    setUp=fixture.BotChecks.setUp
    post=fixture.BotChecks.post
    account=fixture.BotChecks.account
    login=fixture.BotChecks.login
    configure=fixture.BotChecks.configure
    update=fixture.BotChecks.update

    def callback(self, data, *, owner=987654321, chat_type='private', update_id=800):
        return {'update_id':update_id,'callback_query':{'id':'callback-test-'+str(update_id),
            'from':{'id':owner},'message':{'chat':{'id':owner,'type':chat_type}},'data':data}}

    def create(self):
        row,_=self.configure()
        answer=self.manager.command(row,self.update('/new customer | 30 | 10'))
        self.assertEqual(answer[0],'delivery')
        return row,answer

    def test_new_cards_offer_native_controls_and_list_retains_four_actions(self):
        row,created=self.create()
        self.assertEqual(len(created[1]),2)
        self.assertNotIn('reply_markup',created[1][0])
        markup=created[1][1]['reply_markup']['inline_keyboard']
        self.assertEqual([button['text'].split()[-1] for line in markup for button in line],
                         ['Refresh','Edit','Back'])
        answer=self.manager.command(row,self.update('/peers'))
        markup=next(item['reply_markup']['inline_keyboard'] for item in answer[1]
                    if 'inline_keyboard' in item.get('reply_markup',{}))
        self.assertEqual([b['callback_data'] for line in markup for b in line],
                         ['config:1','qr:1','link:1','manage:1'])
        self.assertEqual(self.manager.command(row,self.callback('config:1')),
                         ('document',b'native-config'))
        kind,image=self.manager.command(row,self.callback('qr:1'))
        self.assertEqual(kind,'photo')
        self.assertTrue(image.startswith(b'\x89PNG'))
        self.assertEqual(self.manager.command(row,self.callback('link:1')),
                         ('text','http://localhost/u/opaque-user-token'))

    def test_callbacks_reject_other_users_groups_and_foreign_or_reused_peer(self):
        row,_=self.create()
        for value in (self.callback('config:1',owner=7),self.callback('link:1',chat_type='group')):
            self.assertIsNone(self.manager.command(row,value))
        for action in ('config','qr','link','manage','rename','renew'):
            answer=self.manager.command(row,self.callback(action+':99'))
            self.assertEqual(answer[0],'text')
            self.assertIn('unavailable',answer[1])
        self.rows[0]['public_key']='reused-native-id'
        self.assertIn('unavailable',self.manager.command(row,self.callback('config:1'))[1])

    def test_manage_prompts_use_scoped_native_mutator_and_clear_on_rotation(self):
        row,_=self.create()
        native=NativeApplicationCore(self.app)
        operations=[]
        def dispatch(method,path,payload=None,**kwargs):
            if path=='/api/portal/owned-peer-action':
                operations.append(payload)
                return 200,{'ok':True}
            return native.json(method,path,payload,**kwargs)
        self.app.config['RESELLER_CORE']=SimpleNamespace(json=dispatch,call=native.call)
        self.assertEqual(self.manager.command(row,self.callback('manage:1'))[0],'messages')
        self.manager.command(row,self.callback('rename:1'))
        self.manager.command(row,self.update('new-customer',update_id=801))
        self.assertEqual(operations[-1]['action'],'rename')
        self.assertEqual(operations[-1]['name'],'new-customer')
        self.assertEqual(operations[-1]['public_key'],self.rows[0]['public_key'])
        self.manager.command(row,self.callback('renew:1',update_id=802))
        self.manager.command(row,self.update('5 | 20',update_id=803))
        self.assertEqual(operations[-1]['action'],'renew')
        self.assertEqual(operations[-1]['days'],5)
        self.assertIn('telegram-update-',operations[-1]['request_id'])
        count=len(operations)
        self.manager.command(row,self.update('5 | 20',update_id=803))
        self.assertEqual(len(operations),count,'Repeated prompt reply cannot renew twice')
        self.manager.command(row,self.callback('renew:1',update_id=804))
        new_row,_=self.configure()
        self.manager.command(new_row,self.update('8 | 9',update_id=805))
        self.assertEqual(len(operations),count,'New credential revision must not inherit prompts')

    def test_paused_account_cannot_use_buttons_and_credit_is_scoped(self):
        row,_=self.create()
        credit=self.manager.command(row,self.callback('credit'))
        self.assertIn('Peer',credit[1])
        self.store.update(self.parent['id'],enabled=False)
        self.assertIn('paused',self.manager.command(row,self.callback('config:1'))[1])

    def test_sync_transport_preserves_error_classification_and_encodes_keyboard(self):
        token='123456:'+('A'*35)
        response=SimpleNamespace(content=b'{}',status_code=401,
            json=lambda:{'ok':False,'error_code':401,'description':'do not reflect '+token})
        with patch('portal_reseller_bot.requests.Session') as session:
            client=session.return_value.__enter__.return_value
            client.post.return_value=response
            with self.assertRaises(BotError) as caught:
                TelegramTransport().call(token,'getMe')
            self.assertEqual(caught.exception.code,'telegram_invalid_token')
            self.assertNotIn(token,str(caught.exception))
            response.status_code=200
            response.json=lambda:{'ok':True,'result':True}
            TelegramTransport().call(token,'sendMessage',{'chat_id':1,'reply_markup':{'inline_keyboard':[]}})
            self.assertIsInstance(client.post.call_args.kwargs['data']['reply_markup'],str)


if __name__=='__main__':
    unittest.main()
