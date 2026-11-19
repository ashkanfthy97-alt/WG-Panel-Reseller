import hashlib,hmac,json,sys,tempfile,threading,time,unittest
from pathlib import Path
from urllib.parse import urlencode
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portal_commerce_auth import Auth,AuthenticationError,validate_init_data
from portal_commerce_store import Store

TOKEN='123456:'+('A'*35)
def signed(now=1000,uid=42,**extra):
    data={'auth_date':str(now),'user':json.dumps({'id':uid,'first_name':'مشتری'},ensure_ascii=False),**extra}
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    data['hash']=hmac.new(secret,'\n'.join(k+'='+data[k] for k in sorted(data)).encode(),hashlib.sha256).hexdigest()
    return urlencode(data)

class AuthenticationChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'auth.sqlite3');self.now=1000;self.token=TOKEN
        outer=self
        class Service:
            def customer_from_telegram(self,user,referral_code=None):
                with outer.store.db(write=True) as db:
                    db.execute("INSERT OR IGNORE INTO customers(telegram_id,first_name,referral_code,created_at) VALUES(?,?,?,?)",(user['id'],user['first_name'],'r'+str(user['id']),1000))
                    return dict(db.execute('SELECT * FROM customers WHERE telegram_id=?',(user['id'],)).fetchone())
        self.auth=Auth(self.store,lambda:self.token,Service(),clock=lambda:self.now)
    def test_official_hmac_and_signature_field(self):
        raw=signed(signature='signed-by-telegram')
        self.assertEqual(validate_init_data(raw,TOKEN,now=1000)['id'],42)
        with self.assertRaises(AuthenticationError):validate_init_data(raw.replace('auth_date=1000','auth_date=1001'),TOKEN,now=1000)
        self.assertNotIn('signature',validate_init_data(raw,TOKEN,now=1000))
    def test_malformed_duplicate_stale_future_or_bot_identity_denied(self):
        for raw in [signed()+'&auth_date=1000',signed(now=699),signed(now=1031),signed(uid=True),signed(uid=-1),signed()+'%zz',signed().replace('hash=','hash=zz')]:
            with self.assertRaises(AuthenticationError,msg=raw):validate_init_data(raw,TOKEN,now=1000)
    def test_login_replay_hashed_cookie_expiry_logout_and_rotation(self):
        result=self.auth.login(signed());self.assertEqual(self.auth.authenticate(result['token'])['customer_id'],result['customer']['id'])
        with self.store.db() as db:
            row=dict(db.execute('SELECT * FROM commerce_auth_sessions').fetchone())
            self.assertNotIn(result['token'],str(row));self.assertNotIn(TOKEN,str(row))
        with self.assertRaises(AuthenticationError):self.auth.login(signed())
        self.token='123456:'+('B'*35)
        with self.assertRaises(AuthenticationError):self.auth.authenticate(result['token'])
        self.token=TOKEN;self.now=2800
        with self.assertRaises(AuthenticationError):self.auth.authenticate(result['token'])
        self.now=1000;self.auth.logout(result['token'])
        with self.assertRaises(AuthenticationError):self.auth.authenticate(result['token'])
    def test_customer_suspend_applies_to_existing_session(self):
        result=self.auth.login(signed())
        with self.store.db(write=True) as db:db.execute('UPDATE customers SET suspended=1 WHERE id=?',(result['customer']['id'],))
        with self.assertRaises(AuthenticationError) as error:self.auth.authenticate(result['token'])
        self.assertEqual(error.exception.status,401)
    def test_replay_race_has_one_session(self):
        successes=[];errors=[]
        def go():
            try:successes.append(self.auth.login(signed()))
            except AuthenticationError:errors.append('replayed')
        workers=[threading.Thread(target=go) for _ in range(2)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        self.assertEqual(len(successes),1);self.assertEqual(len(errors),1)

if __name__=='__main__':unittest.main()
