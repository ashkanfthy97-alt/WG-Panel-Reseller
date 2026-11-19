"""Exercise non-TESTING configuration paths with fake Telegram, no network."""
import ast
import contextlib
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from flask import Flask,jsonify,session,request,Response,abort
from portal_commerce import install_commerce,CommerceHTTPError
from portal_commerce_auth import Auth
from portal_commerce_service import CommerceService
from portal_commerce_store import Store

TOKEN='123456:abcdefghijklmnopqrstuvwxyz_fixture_123'

class TelegramFailure(ValueError):
    def __init__(self,code,retry_after=0):self.code,self.retry_after=code,retry_after

class FakeTelegram:
    def __init__(self):self.calls=[];self.fail=None;self.fail_method=None;self.chat_id=12345
    def call(self,token,method,payload=None,**kwargs):
        self.calls.append((method,payload,kwargs))
        if self.fail and (self.fail_method is None or self.fail_method==method):raise self.fail
        if method=='getMe':return {'id':123456,'is_bot':True,'username':'fixture_bot'}
        if method=='getWebhookInfo':return {'url':''}
        if method=='getChat':return {'id':self.chat_id,'type':'private'}
        raise AssertionError(method)

class Native:
    def __init__(self,*args):pass
    def targets(self):return [{'scope':'local','iface_id':1}]

class BotSetup(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.transport=FakeTelegram()
        app=self.app=Flask('commerce-production-style-setup',instance_path=self.tmp.name)
        app.config.update(TESTING=True,SECRET_KEY='temporary-setup-secret',
            COMMERCE_STORE_FACTORY=Store,COMMERCE_SERVICE_FACTORY=CommerceService,
            COMMERCE_AUTH_FACTORY=Auth,COMMERCE_NATIVE_FACTORY=Native,
            COMMERCE_TELEGRAM_TRANSPORT=self.transport)
        @app.get('/api/admin')
        def native_owner():return (jsonify(username='mother'),200) if session.get('native_owner') else (jsonify(error='login_required'),401)
        @app.before_request
        def external_policy():
            if request.headers.get('X-Test-Upstream-Failure'):
                return Response('<!doctype html><p>private upstream information</p>',status=502,mimetype='text/html')
        # Execute the actual native hook without importing the full Linux app.
        # Manual Flask-WTF protect() rejects requests without its native token.
        csrf=SimpleNamespace(protect=lambda:abort(400),exempt=lambda blueprint:None)
        source=ast.parse((ROOT/'app.py').read_text(encoding='utf-8'))
        hook=next(node for node in source.body if isinstance(node,ast.FunctionDef) and node.name=='_csrf_protect_ui')
        exec(compile(ast.Module(body=[hook],type_ignores=[]),'app.py','exec'),{'app':app,'request':request,'csrf':csrf})
        app.add_url_rule('/native-form',view_func=lambda:'ok',methods=['POST'])
        install_commerce(app,csrf)
        self.runtime=app.extensions['portal_commerce_runtime_config']
        self.service=app.extensions['portal_commerce_service']
        # Setup is now complete. Exercise the real production validation path.
        app.config['TESTING']=False
        self.client=app.test_client()
        with self.client.session_transaction(base_url='https://localhost') as value:value['native_owner']=True
        self.csrf=self.client.get('/telegram-commerce/api/settings',base_url='https://localhost').json['csrf']
    def save(self,value):
        return self.client.put('/telegram-commerce/api/settings',json=value,base_url='https://localhost',headers={
            'Origin':'https://localhost','X-Commerce-CSRF':self.csrf,'Idempotency-Key':'temporary-setup-operation-123'})
    def payload(self,**values):
        return {'bot_token':TOKEN,'bot_enabled':True,'public_base_url':'https://localhost','manager_chat_id':'۱۲۳۴۵',**values}
    def test_production_verification_and_numeric_destination(self):
        result=self.save(self.payload())
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['settings']['manager_chat_id'],12345)
        self.assertNotIn(TOKEN,result.get_data(as_text=True))
        self.assertEqual([item[0] for item in self.transport.calls],['getMe','getWebhookInfo','getChat'])
        self.assertEqual(self.transport.calls[-1][1],{'chat_id':12345})
        self.assertTrue(all(item[2].get('timeout')==(3,5) for item in self.transport.calls))
        public=self.app.test_client()
        public.set_cookie('wgportal_customer','12345',path='/telegram-app')
        self.assertEqual(public.get('/telegram-commerce/api/settings',base_url='https://localhost').status_code,401)
    def test_commerce_and_native_forms_keep_their_own_csrf_checks(self):
        for origin, token, error in [('https://other.invalid',self.csrf,'invalid_origin'),('https://localhost','incorrect','invalid_csrf')]:
            result=self.client.put('/telegram-commerce/api/settings',json=self.payload(),base_url='https://localhost',headers={
                'Origin':origin,'X-Commerce-CSRF':token,'Idempotency-Key':'temporary-setup-operation-123'})
            self.assertEqual(result.status_code,403)
            self.assertEqual(result.json['error'],error)
        self.assertEqual(self.client.post('/native-form',base_url='https://localhost').status_code,400)
        self.assertEqual(self.transport.calls,[])
    def test_missing_invalid_or_oversized_id_rejected(self):
        for identity in ['',True,1.2,-12,'-12345','0',123456,str(2**52),'999999999999999999999999999999']:
            with self.subTest(identity=identity):
                result=self.save(self.payload(manager_chat_id=identity))
                self.assertEqual(result.status_code,400,result.json)
                self.assertEqual(result.json['error'],'invalid_manager_chat_id')
    def test_failed_validation_does_not_partially_save_business_settings(self):
        self.transport.fail=TelegramFailure('telegram_invalid_token')
        result=self.save(self.payload(referral_percent=37))
        self.assertEqual(result.status_code,400,result.json)
        self.assertEqual(result.json['error'],'invalid_bot_token')
        self.assertEqual(self.service.admin_action('mother','settings')['settings']['referral_percent'],0)
        self.assertFalse(self.runtime.public()['bot_configured'])
    def test_rate_limit_and_unknown_verification_failure_are_json(self):
        for failure,status in [(TelegramFailure('telegram_rate_limited',47),429),(RuntimeError('never echo '+TOKEN),503)]:
            self.transport.fail=failure
            result=self.save(self.payload())
            self.assertEqual(result.status_code,status,result.get_data(as_text=True))
            self.assertTrue(result.is_json);self.assertNotIn(TOKEN,result.get_data(as_text=True))
    def test_unknown_chat_requests_start_without_saving(self):
        self.transport.chat_id=999
        result=self.save(self.payload())
        self.assertEqual(result.status_code,400,result.json)
        self.assertEqual(result.json['error'],'manager_chat_unavailable')
        self.assertFalse(self.runtime.public()['bot_configured'])
    def test_runtime_database_failure_never_returns_html_or_secret(self):
        @contextlib.contextmanager
        def broken():raise sqlite3.OperationalError('sensitive '+TOKEN);yield
        self.app.extensions['reseller_bots']=SimpleNamespace(store=SimpleNamespace(db=broken))
        result=self.save(self.payload())
        self.assertTrue(result.is_json,result.get_data(as_text=True))
        self.assertEqual(result.status_code,503,result.json)
        self.assertNotIn(TOKEN,result.get_data(as_text=True))
    def test_invalid_business_setting_does_not_save_verified_runtime(self):
        result=self.save(self.payload(referral_percent=101))
        self.assertEqual(result.status_code,400,result.json)
        self.assertEqual([x[0] for x in self.transport.calls],['getMe','getWebhookInfo','getChat'])
        self.assertFalse(self.runtime.public()['bot_configured'])
    def test_runtime_write_failure_rolls_back_business_settings(self):
        with patch.object(self.runtime,'write',side_effect=sqlite3.OperationalError('secret '+TOKEN)):
            result=self.save(self.payload(referral_percent=37))
        self.assertEqual(result.status_code,503,result.json)
        self.assertEqual(self.service.admin_action('mother','settings')['settings']['referral_percent'],0)
        self.assertFalse(self.runtime.public()['bot_configured'])
    def test_manager_not_started_and_webhook_or_token_failure(self):
        self.transport.fail=TelegramFailure('telegram_bad_request');self.transport.fail_method='getChat'
        result=self.save(self.payload())
        self.assertEqual(result.status_code,400,result.json)
        self.assertEqual(result.json['error'],'manager_chat_unavailable')
        self.assertIn('/start',result.json['message'])
    def test_numeric_manager_routes_notifications_without_global_registry_write(self):
        result=self.save(self.payload());self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.runtime.notification_recipients([45678,12345,'-8','invalid']),[12345,45678])
        with self.app.extensions['portal_commerce_store'].db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_admins'").fetchone())
        self.assertEqual(self.transport.calls[-1][0],'getChat')
    def test_manager_notification_does_not_depend_on_native_registry(self):
        from portal_commerce import native_admin_notification_recipients
        result=self.save(self.payload());self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(native_admin_notification_recipients(self.app,self.runtime),[12345])
        self.app.config['API_KEY']='temporary-native-key'
        self.assertEqual(native_admin_notification_recipients(self.app,self.runtime),[12345])
    def test_native_html_failure_is_json_with_original_http_status(self):
        response=self.client.get('/telegram-commerce/api/settings',base_url='https://localhost',headers={'X-Test-Upstream-Failure':'yes'})
        self.assertEqual(response.status_code,502)
        self.assertTrue(response.is_json)
        self.assertNotIn('private upstream',response.get_data(as_text=True))
    def test_oversized_bot_identity_is_rejected_before_sqlite_or_telegram(self):
        result=self.save(self.payload(bot_token='99999999999999999999:abcdefghijklmnopqrstuvwxyz_fixture_123'))
        self.assertEqual(result.status_code,400,result.json)
        self.assertEqual(result.json['error'],'invalid_bot_token')
        self.assertEqual(self.transport.calls,[])

if __name__=='__main__':unittest.main()
