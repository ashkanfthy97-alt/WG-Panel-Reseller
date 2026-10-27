"""Temporary stores, real credential encryption, fake Telegram/native transport."""
from pathlib import Path
import sys
import logging
import threading
import unittest
from unittest.mock import patch
from flask import jsonify

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import test_native_reseller as fixture
from portal_reseller_bot import protect_transport_logs, BotManager, BotError


class FakeTelegram:
    def __init__(self):
        self.calls=[]
    def call(self,token,method,payload=None,**kwargs):
        self.calls.append(method)
        if method=='getMe':
            return {'id':int(token.split(':')[0]),'is_bot':True,'username':'dealer_config_bot'}
        if method=='getWebhookInfo':
            return {'url':''}
        raise AssertionError('Unexpected live polling')


class BotChecks(unittest.TestCase):
    post=fixture.NativeResellerChecks.post
    account=fixture.NativeResellerChecks.account
    login=fixture.NativeResellerChecks.login

    def setUp(self):
        fixture.NativeResellerChecks.setUp(self)
        self.app.config['RESELLER_BOT_CSRF_FACTORY']=lambda:'native-fixture-csrf'
        self.transport=FakeTelegram()
        self.app.config['RESELLER_BOT_TRANSPORT']=self.transport
        self.manager=self.app.extensions['reseller_bots']
        self.parent=self.account()
        self.login()

    def configure(self):
        token='123456:'+('A'*35)
        result=self.post('/reseller/api/bot',{'owner_id':'987654321','token':token})
        self.assertEqual(result.status_code,200,result.get_data(as_text=True))
        self.assertNotIn(token,result.get_data(as_text=True))
        self.assertNotIn('token_enc',result.get_data(as_text=True))
        return self.manager.store.get(self.parent['id']),token

    def update(self,text,*,owner=987654321,chat_type='private',update_id=501):
        return {'update_id':update_id,'message':{'text':text,'from':{'id':owner},
            'chat':{'id':owner,'type':chat_type}}}

    def test_encrypted_storage_safe_status_and_disable(self):
        row,token=self.configure()
        self.assertNotIn(token,self.manager.store.path.read_bytes().decode('latin1'))
        self.assertEqual(self.manager.store.cipher().decrypt(row['token_enc'].encode()).decode(),token)
        status=self.client.get('/reseller/api/bot').json
        self.assertEqual(status['native_csrf'],'native-fixture-csrf')
        self.assertTrue(status['bot']['configured'])
        self.assertFalse(self.manager.started,'Testing must never start polling')
        response=self.client.delete('/reseller/api/bot',headers={'Origin':'http://localhost','X-Reseller-CSRF':self.csrf})
        self.assertEqual(response.status_code,200)
        self.assertIsNone(self.manager.current(row))
        self.assertEqual(self.manager.store.get(self.parent['id'])['token_enc'],'')

    def test_private_owner_only_and_create_replay_owned_link(self):
        row,_=self.configure()
        for value in [self.update('/new customer | 30 | 10',owner=11),self.update('/new customer | 30 | 10',chat_type='group')]:
            self.assertIsNone(self.manager.command(row,value))
        self.assertEqual(self.rows,[])
        update=self.update('/new customer | 30 | 10')
        self.assertIn('Peer ID: 1',self.manager.command(row,update)[1])
        self.assertIn('Peer ID: 1',self.manager.command(row,update)[1])
        self.assertEqual(len(self.rows),1,'Repeated Telegram update must not create another peer')
        self.assertEqual(self.manager.command(row,self.update('/config 1'))[1],b'native-config')
        self.assertEqual(self.manager.command(row,self.update('/link 1'))[1],'http://localhost/u/opaque-user-token')
        self.rows[0]['public_key']='replaced-key'
        self.assertNotIn('opaque-user-token',self.manager.command(row,self.update('/link 1'))[1])

    def test_pause_and_rotation_block_old_worker(self):
        row,_=self.configure()
        self.store.update(self.parent['id'],enabled=False)
        self.assertIn('paused',self.manager.command(row,self.update('/new customer | 30 | 10'))[1])
        self.assertFalse(self.rows)
        self.store.update(self.parent['id'],enabled=True)
        new_row,_=self.configure()
        self.assertNotEqual(row['revision'],new_row['revision'])
        self.assertIsNone(self.manager.current(row))

    def test_bot_credentials_require_origin_csrf_and_active_account(self):
        self.assertEqual(self.client.post('/reseller/api/bot',json={}).status_code,403)
        self.assertEqual(self.client.post('/reseller/api/bot',json={},headers={'Origin':'http://evil.test','X-Reseller-CSRF':self.csrf}).status_code,403)
        self.assertFalse(self.transport.calls)
        self.store.approve(self.parent['id'],approved=False)
        self.assertEqual(self.client.get('/reseller/api/bot').status_code,403)

    def test_debug_transport_log_redacts_token(self):
        protect_transport_logs()
        token='123456:'+('A'*35)
        with self.assertLogs('urllib3.connectionpool',level=logging.DEBUG) as captured:
            logging.getLogger('urllib3.connectionpool').debug('POST %s', '/bot'+token+'/getUpdates')
        self.assertNotIn(token,' '.join(captured.output))
        self.assertIn('[REDACTED]',' '.join(captured.output))

    def test_poll_restart_keeps_offset_after_delivery_failure(self):
        row,_=self.configure()
        update=self.update('/new customer | 30 | 10')
        class StopOnBackoff(threading.Event):
            def wait(self, timeout=None):
                self.set()
                return True
        stop=StopOnBackoff()
        class FailedDelivery:
            def call(inner,token,method,payload=None,**kwargs):
                if method=='getUpdates':
                    return [update]
                if method=='sendMessage':
                    raise BotError('simulated_delivery_failure')
                raise AssertionError(method)
        self.app.config['RESELLER_BOT_TRANSPORT']=FailedDelivery()
        self.manager.poll(row,stop)
        self.assertEqual(len(self.rows),1)
        saved=self.manager.store.get(self.parent['id'])
        self.assertEqual(saved['offset'],502)
        restarted=BotManager(self.app,self.database)
        observed=[]
        stop=threading.Event()
        class Resume:
            def call(inner,token,method,payload=None,**kwargs):
                self.assertEqual(method,'getUpdates')
                observed.append(payload['offset'])
                stop.set()
                return []
        self.app.config['RESELLER_BOT_TRANSPORT']=Resume()
        restarted.poll(saved,stop)
        self.assertEqual(observed,[502])
        self.assertEqual(len(self.rows),1)

    def test_webhook_is_not_taken_over_and_same_bot_cannot_be_reassigned(self):
        row,token=self.configure()
        other=self.store.create('other','a-safe-test-password',[2])
        with self.assertRaises(ValueError):
            self.manager.store.save(other['id'],123,token,{'id':123456,'username':'dealer_config_bot'},'http://localhost')
        with patch.object(self.transport,'call',side_effect=[{'id':123456,'is_bot':True,'username':'dealer_config_bot'},{'url':'https://existing.example/hook'}]):
            result=self.post('/reseller/api/bot',{'owner_id':'987654321','token':token})
        self.assertEqual(result.status_code,409)
        self.assertEqual(self.manager.store.get(self.parent['id'])['revision'],row['revision'])


if __name__=='__main__':
    unittest.main()
