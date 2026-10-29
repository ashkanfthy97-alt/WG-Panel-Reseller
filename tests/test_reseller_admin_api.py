"""Native administrator-bot bridge with mock native API and temporary data."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from flask import jsonify,request
from werkzeug.routing import Rule
import test_native_reseller as fixture
from portal_reseller_native import NativeApplicationCore
from deploy.linux.portal_reseller import CoreError


class ResellerAdminApiChecks(unittest.TestCase):
    account=fixture.NativeResellerChecks.account
    post=fixture.NativeResellerChecks.post
    login=fixture.NativeResellerChecks.login
    def setUp(self):
        fixture.NativeResellerChecks.setUp(self)
        # The native API exists in the production app; add its fake after the
        # common fixture has made a session request, without touching real data.
        self.app.url_map.add(Rule('/api/telegram/admins',endpoint='fixture_tg_admins'))
        def admins():
            if request.headers.get('X-API-KEY')!=self.app.config['API_KEY']:
                return jsonify(error='denied'),401
            return jsonify(admins=[{'id':'333','username':'mother'}])
        self.app.view_functions['fixture_tg_admins']=admins

    def bridge(self,action,payload=None,key=True,actor='333',request_id='telegram-test-request-0001'):
        return self.client.post('/api/portal/reseller-admin',json={'action':action,'payload':payload or {},'actor':{'id':actor,'username':'claimed'},'request_id':request_id},headers={'X-API-KEY':self.app.config['API_KEY']} if key else {})

    def test_key_and_current_telegram_admin_required_and_reseller_dispatch_denied(self):
        self.account();self.login()
        self.assertEqual(self.bridge('list',key=False).status_code,401)
        self.assertEqual(self.bridge('list',actor='999').status_code,403)
        self.assertEqual(self.bridge('not-allowed').status_code,400)
        with self.assertRaises(CoreError):
            NativeApplicationCore(self.app).json('POST','/api/portal/reseller-admin',{'action':'list'})

    def test_owner_actions_use_shared_approval_provisioning_and_idempotent_renewal(self):
        created=self.bridge('create',{'username':'native-bot-dealer','password':'long-native-bot-password','max_peers':9})
        self.assertEqual(created.status_code,201,created.json)
        account=created.json['account'];account_id=account['id']
        self.assertFalse(account['active'])
        self.assertEqual(created.json['interface']['listen_port'],51825)
        approval=self.bridge('approve',{'id':account_id})
        self.assertEqual(approval.status_code,200,approval.json)
        self.assertTrue(approval.json['account']['active'])
        first=self.bridge('renew',{'id':account_id,'days':5})
        second=self.bridge('renew',{'id':account_id,'days':5})
        self.assertEqual(first.status_code,200,first.json)
        self.assertEqual(first.json['account']['expires_at'],second.json['account']['expires_at'])
        self.assertEqual(self.bridge('renew',{'id':account_id,'days':6}).status_code,400)
        self.assertFalse(self.bridge('disable',{'id':account_id}).json['account']['enabled'])
        self.assertTrue(self.bridge('enable',{'id':account_id}).json['account']['enabled'])
        listing=self.bridge('list')
        self.assertEqual(listing.status_code,200,listing.json)
        self.assertNotIn('csrf',listing.json)
        self.assertTrue(any(row['actor']=='telegram-admin:333' for row in self.store.audit()))

    def test_archive_cleanup_failure_is_truthful_idempotent_and_preserves_peers_children(self):
        parent=self.account()
        self.app.extensions['reseller_bots']=SimpleNamespace(store=SimpleNamespace(disable=Mock(side_effect=RuntimeError('temporary credential DB failure'))))
        result=self.bridge('archive',{'id':parent['id'],'confirm':True})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['warning'],'bot_cleanup_pending')
        self.assertTrue(result.json['account']['deleted'])
        self.assertTrue(result.json['resources_preserved'])
        self.assertEqual(self.bridge('archive',{'id':parent['id'],'confirm':True}).status_code,200)
        self.assertEqual(self.bridge('archive',{'id':parent['id']}).status_code,400)

    def test_malformed_or_unavailable_registry_fails_closed(self):
        self.app.view_functions['fixture_tg_admins']=lambda:jsonify(admins=None)
        self.assertEqual(self.bridge('list').status_code,403)
        self.app.view_functions['fixture_tg_admins']=lambda:jsonify(admins=[{'id':'333'}])
        self.assertEqual(self.bridge(['not-a-string']).status_code,400)
        self.assertEqual(self.bridge('list',request_id={}).status_code,400)

    def test_reset_receipt_preserves_later_enrollment_and_rejects_other_actor(self):
        from portal_reseller_security import setup, change
        from test_reseller_security import code
        account=self.account()
        payload={'id':account['id'],'confirm':True}
        self.assertEqual(self.bridge('reset2fa',payload,request_id='').status_code,400)
        first=self.bridge('reset2fa',payload,request_id='telegram-reset-operation-0001')
        self.assertEqual(first.status_code,200,first.json)
        with self.app.app_context():
            enrollment=setup(self.store,account['id'],'a-safe-test-password','reset-reenroll-fixture')
            change(self.store,account['id'],'a-safe-test-password',code(enrollment['secret']),'reset-reenroll-fixture',enable=True)
        reenrolled=self.store.get(account['id'])
        self.assertTrue(reenrolled['twofa_enabled'])
        repeat=self.bridge('reset2fa',payload,request_id='telegram-reset-operation-0001')
        self.assertEqual(repeat.status_code,200,repeat.json)
        self.assertTrue(repeat.json['account']['twofa_enabled'])
        self.assertEqual(repeat.json['account']['auth_version'],reenrolled['auth_version'])
        self.app.view_functions['fixture_tg_admins']=lambda:jsonify(admins=[{'id':'333'},{'id':'444'}])
        conflict=self.bridge('reset2fa',payload,actor='444',request_id='telegram-reset-operation-0001')
        self.assertEqual(conflict.status_code,400)
        self.assertTrue(self.store.get(account['id'])['twofa_enabled'])
        with self.store.db() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM reseller_security_resets').fetchone()[0],1)

    def test_defaults_quota_and_reset_confirmation(self):
        account=self.account()
        self.assertEqual(self.bridge('defaults_get').status_code,200)
        self.assertEqual(self.bridge('quota',{'id':account['id'],'max_peers':2}).status_code,200)
        self.assertEqual(self.bridge('reset2fa',{'id':account['id']}).status_code,400)
        old=account['auth_version']
        reset=self.bridge('reset2fa',{'id':account['id'],'confirm':True})
        self.assertEqual(reset.status_code,200,reset.json)
        self.assertGreater(reset.json['account']['auth_version'],old)


if __name__=='__main__':
    unittest.main()
