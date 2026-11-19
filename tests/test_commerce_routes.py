"""Customer and owner HTTP boundaries; no production services."""
import io, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask
from portal_commerce import install_commerce, save_image, CommerceHTTPError

class FakeStore:
    def __init__(self,path): self.path=path
    def initialize(self): pass
class Service:
    def __init__(self,store,native_adapter=None): self.store=store; self.calls=[]
    def customer_action(self,cid,action,payload=None,request_id=None):
        self.calls.append((cid,action,payload,request_id));return {'action':action}
    def admin_action(self,actor,action,payload=None,request_id=None):
        self.calls.append((actor,action,payload,request_id));return {'action':action}
class Auth:
    def __init__(self,*args): pass
    def authenticate(self,token):return {'customer_id':7,'csrf':'verified-csrf','customer':{'id':7}} if token=='verified' else None
    def login(self,raw):return {'token':'verified','csrf':'verified-csrf','customer':{'id':7}}
    def logout(self,token):pass

class Routes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=Flask(__name__,instance_path=self.tmp.name);self.app.config.update(TESTING=True,SECRET_KEY='test',COMMERCE_STORE_FACTORY=FakeStore,COMMERCE_SERVICE_FACTORY=Service,COMMERCE_AUTH_FACTORY=Auth,COMMERCE_NATIVE_FACTORY=lambda app,store:None,COMMERCE_OWNER_CHECK=lambda:None)
        @self.app.after_request
        def native_headers(response):
            response.headers['X-Frame-Options']='DENY'
            return response
        install_commerce(self.app,None);self.client=self.app.test_client()
    def test_customer_cookie_never_grants_owner(self):
        self.client.set_cookie('wgportal_customer','verified',path='/telegram-app')
        self.assertEqual(self.client.get('/telegram-app/api/dashboard').status_code,200)
        self.assertEqual(self.client.get('/telegram-commerce/api/dashboard').status_code,401)
        call=self.app.extensions['portal_commerce_service'].calls[-1]
        self.assertEqual(call[:2],(7,'dashboard'))
    def test_mutation_requires_csrf_origin_and_receipt(self):
        self.client.set_cookie('wgportal_customer','verified',path='/telegram-app')
        path='/telegram-app/api/orders'
        self.assertEqual(self.client.post(path,json={}).status_code,403)
        headers={'Origin':'http://localhost','X-Commerce-CSRF':'verified-csrf','Idempotency-Key':'temporary-order-key-1234'}
        result=self.client.post(path,json={'quote_id':1,'payment_method':'card'},headers=headers)
        self.assertEqual(result.status_code,200,result.json)
        headers['Origin']='https://attacker.test';self.assertEqual(self.client.post(path,json={},headers=headers).status_code,403)
    def test_auth_cookie_security_and_no_token_in_body(self):
        result=self.client.post('/telegram-app/api/auth',json={'init_data':'verified-fixture'},headers={'Origin':'https://localhost'},base_url='https://localhost')
        self.assertEqual(result.status_code,200);self.assertNotIn('token',result.json)
        cookie=result.headers['Set-Cookie'];self.assertIn('Secure',cookie);self.assertIn('HttpOnly',cookie);self.assertIn('Path=/telegram-app',cookie)
    def test_unauthenticated_no_data(self):
        self.assertEqual(self.client.get('/telegram-app/api/orders/1').status_code,401)
        self.assertEqual(self.client.get('/telegram-commerce/api/payment-destinations').status_code,401)
    def test_telegram_embedding_headers_survive_native_policy(self):
        response=self.client.post('/telegram-app/api/auth',json={'init_data':'verified-fixture'},base_url='https://localhost',headers={'Origin':'https://localhost'})
        self.assertNotIn('X-Frame-Options',response.headers)
        self.assertIn('frame-ancestors https://web.telegram.org',response.headers['Content-Security-Policy'])
    def test_receipt_actual_image_and_retry_reference(self):
        from PIL import Image
        from werkzeug.datastructures import FileStorage
        with self.app.app_context():
            with self.assertRaises(CommerceHTTPError):
                save_image(FileStorage(stream=io.BytesIO(b'<svg onload="alert(1)"/>'),filename='receipt.png'),Path(self.tmp.name))
            raw=io.BytesIO();Image.new('RGB',(8,8),'white').save(raw,format='PNG')
            def upload():return FileStorage(stream=io.BytesIO(raw.getvalue()),filename='../../unsafe.html')
            one=save_image(upload(),Path(self.tmp.name),identity='7:temporary-upload-key')
            two=save_image(upload(),Path(self.tmp.name),identity='7:temporary-upload-key')
            self.assertEqual(one,two)
            self.assertEqual(len(list((Path(self.tmp.name)/'receipts').iterdir())),1)
            self.assertTrue((Path(self.tmp.name)/'receipts'/one).is_file())
    def test_runtime_waits_until_all_native_registration_finishes(self):
        from unittest.mock import patch
        from portal_commerce import install_runtime_starter
        application=Flask('runtime-registration-fixture')
        with patch('portal_commerce.threading.Thread') as thread:
            install_runtime_starter(application,None,None,Path(self.tmp.name))
            thread.assert_not_called()
            @application.get('/native-final-route')
            def final_route():return 'registered'
            client=application.test_client()
            self.assertEqual(client.get('/native-final-route').status_code,200)
            self.assertEqual(client.get('/native-final-route').status_code,200)
            self.assertEqual(thread.call_count,1)

if __name__=='__main__':unittest.main()
