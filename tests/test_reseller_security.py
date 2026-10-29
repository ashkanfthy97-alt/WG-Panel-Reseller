"""Temporary reseller TOTP checks; no real account, server or authenticator."""
import base64
import hashlib
import hmac
import json
import struct
import time
import unittest
from unittest.mock import patch
import test_native_reseller as fixture
from portal_reseller_security import counter_for


def code(secret, at=None):
    counter=int(time.time() if at is None else at)//30
    key=base64.b32decode(secret+'='*(-len(secret)%8))
    digest=hmac.new(key,struct.pack('>Q',counter),hashlib.sha1).digest()
    offset=digest[-1]&15
    return f"{(struct.unpack('>I',digest[offset:offset+4])[0]&0x7fffffff)%1000000:06d}"


class ResellerSecurityChecks(unittest.TestCase):
    setUp=fixture.NativeResellerChecks.setUp
    post=fixture.NativeResellerChecks.post
    account=fixture.NativeResellerChecks.account
    login=fixture.NativeResellerChecks.login

    def enroll(self):
        self.parent=self.account()
        self.login()
        setup=self.post('/reseller/api/security/setup',{'password':'a-safe-test-password'})
        self.assertEqual(setup.status_code,200,setup.json)
        secret=setup.json['secret']
        self.assertIn('otpauth://totp/',setup.json['otpauth_uri'])
        enabled=self.post('/reseller/api/security/enable',{'password':'a-safe-test-password','otp':code(secret)})
        self.assertEqual(enabled.status_code,200,enabled.json)
        return secret,enabled.json['recovery_codes']

    def json_login(self, otp=None):
        self.post('/reseller/api/logout',{})
        self.csrf=self.client.get('/reseller/api/session').json['csrf']
        return self.post('/reseller/api/login',{'username':'dealer','password':'a-safe-test-password','otp':otp})

    def test_known_totp_vector_and_persian_digits(self):
        secret=base64.b32encode(b'12345678901234567890').decode().rstrip('=')
        self.assertEqual(counter_for(secret,'287082',now=59),1)
        self.assertEqual(counter_for(secret,'۲۸۷۰۸۲',now=59),1)
        self.assertIsNone(counter_for(secret,'287082',previous=1,now=59))

    def test_enrollment_encrypted_no_public_secrets_and_recovery_once(self):
        secret,recovery=self.enroll()
        self.assertEqual(len(recovery),10)
        status=self.client.get('/reseller/api/security').json
        self.assertEqual(status,{'enabled':True,'recovery_codes_remaining':10})
        self.assertNotIn(secret,self.database.read_bytes().decode('latin1'))
        with self.store.db() as connection:
            row=dict(connection.execute('SELECT * FROM reseller_security').fetchone())
            self.assertNotIn(recovery[0],row['recovery_hashes'])
            self.assertTrue(row['secret_enc'])
        public=json.dumps(self.store.all())
        for forbidden in ('secret_enc','pending_enc','recovery_hashes',secret):
            self.assertNotIn(forbidden,public)
        self.assertEqual(self.json_login().status_code,401)
        self.assertEqual(self.json_login(recovery[0]).status_code,200)
        self.assertEqual(self.json_login(recovery[0]).status_code,401)
        self.assertEqual(self.json_login(recovery[1]).status_code,200)

    def test_both_form_and_json_login_require_second_factor_and_reject_replay(self):
        secret,recovery=self.enroll()
        self.assertEqual(self.json_login(code(secret)).status_code,401,'Enrollment code was already consumed')
        fresh=code(secret,time.time()+30)
        self.assertEqual(self.json_login(fresh).status_code,200)
        self.assertEqual(self.json_login(fresh).status_code,401)
        with patch('portal_reseller_native.render_template',return_value='login-screen'):
            failed=self.client.post('/login',data={'username':'dealer','password':'a-safe-test-password'})
            self.assertEqual(failed.status_code,200)
            self.assertNotEqual(failed.headers.get('Location'),'/reseller')
            success=self.client.post('/login',data={'username':'dealer','password':'a-safe-test-password','twofa_code':recovery[0]})
            self.assertEqual(success.status_code,302)
            self.assertEqual(success.location,'/reseller')
            replay=self.client.post('/login',data={'username':'dealer','password':'a-safe-test-password','twofa_code':recovery[0]})
            self.assertEqual(replay.status_code,200)
            self.assertIsNone(replay.headers.get('Location'))

    def test_setup_requires_password_enable_requires_otp_disable_reauth_and_revokes_other_sessions(self):
        self.parent=self.account();self.login()
        self.assertEqual(self.post('/reseller/api/security/setup',{'password':'wrong'}).status_code,400)
        result=self.post('/reseller/api/security/setup',{'password':'a-safe-test-password'})
        self.assertEqual(self.post('/reseller/api/security/enable',{'password':'a-safe-test-password','otp':'000000'}).status_code,400)
        enabled=self.post('/reseller/api/security/enable',{'password':'a-safe-test-password','otp':code(result.json['secret'])})
        self.assertEqual(enabled.status_code,200)
        self.assertNotEqual(self.store.get(self.parent['id'])['auth_version'],self.parent['auth_version'])
        self.assertIsNotNone(self.client.get('/reseller/api/session').json['account'],'Current session refreshed')
        self.assertEqual(self.post('/reseller/api/security/disable',{'password':'wrong','otp':enabled.json['recovery_codes'][0]}).status_code,400)
        disabled=self.post('/reseller/api/security/disable',{'password':'a-safe-test-password','otp':enabled.json['recovery_codes'][0]})
        self.assertEqual(disabled.status_code,200)
        self.assertFalse(disabled.json['enabled'])
        self.assertEqual(self.json_login().status_code,200)

    def test_enabling_revokes_existing_browser_and_concurrent_recovery_is_once(self):
        import concurrent.futures
        from portal_reseller_native import ResellerSessions
        self.parent=self.account();self.login()
        old=self.client.get_cookie(ResellerSessions.cookie_name).value
        setup=self.post('/reseller/api/security/setup',{'password':'a-safe-test-password'})
        enabled=self.post('/reseller/api/security/enable',{'password':'a-safe-test-password','otp':code(setup.json['secret'])})
        recovery=enabled.json['recovery_codes'][0]
        other=self.app.test_client();other.set_cookie(ResellerSessions.cookie_name,old)
        self.assertIsNone(other.get('/reseller/api/session').json['account'])
        def attempt():
            with self.app.app_context():
                return self.store.authenticate('dealer','a-safe-test-password','parallel-test',otp=recovery)[0] is not None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(sorted(outcomes),[False,True])

    def test_additive_v6_upgrade_preserves_account_and_ownership(self):
        account=self.account()
        receipt=self.store.reserve(account['id'],'security-migration-receipt')
        self.store.finish(receipt['id'],account['id'],41,'M'*43+'=')
        with self.store.db(write=True) as connection:
            password=connection.execute('SELECT password_hash FROM resellers WHERE id=?',(account['id'],)).fetchone()[0]
            connection.execute('DROP TABLE reseller_security_attempts')
            connection.execute('DROP TABLE reseller_security')
            connection.execute('PRAGMA user_version=6')
        fresh=self.store.get(account['id'])
        self.assertEqual(fresh['interfaces'],account['interfaces'])
        self.assertEqual(self.store.ownership(account['id']),{41:'M'*43+'='})
        with self.store.db() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],7)
            self.assertEqual(connection.execute('SELECT password_hash FROM resellers WHERE id=?',(account['id'],)).fetchone()[0],password)
        self.assertFalse(fresh['twofa_enabled'])

    def test_expired_setup_and_rate_limit_fail_closed(self):
        account=self.account();self.login()
        result=self.post('/reseller/api/security/setup',{'password':'a-safe-test-password'})
        with self.store.db(write=True) as connection:
            connection.execute('UPDATE reseller_security SET pending_until=0')
        self.assertEqual(self.post('/reseller/api/security/enable',{'password':'a-safe-test-password','otp':code(result.json['secret'])}).status_code,400)
        with self.store.db(write=True) as connection:
            connection.executemany('INSERT INTO reseller_security_attempts VALUES(?,?,?)',[(account['id'],'test',int(time.time()))]*20)
        self.assertEqual(self.post('/reseller/api/security/setup',{'password':'a-safe-test-password'}).status_code,400)
        self.assertFalse(self.client.get('/reseller/api/security').json['enabled'])


if __name__=='__main__':
    unittest.main()
