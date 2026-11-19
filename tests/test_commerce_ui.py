"""Render real templates; Playwright serves only local fixtures and mocked APIs."""
import json
import os
import hashlib
import hmac
import sys
import threading
import time
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode
from flask import Flask, render_template, request, send_file, abort
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path(os.environ.get('SHELL_SUPPORT_ROOT',ROOT))


@unittest.skipUnless(shutil.which('node') and os.environ.get('PLAYWRIGHT_MODULE_PATH'), 'Existing browser runtime required')
class CommerceBrowser(unittest.TestCase):
    def test_customer_and_owner_flows_at_phone_and_desktop_sizes(self):
        self.assertTrue(shutil.which('node'), 'Existing Node runtime required')
        self.assertTrue(os.environ.get('PLAYWRIGHT_MODULE_PATH'), 'Existing Playwright runtime required')
        app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        app.jinja_loader = ChoiceLoader([FileSystemLoader(ROOT / 'templates'), FileSystemLoader(SHELL / 'templates')])
        app.jinja_env.globals.update(
            current_user=SimpleNamespace(is_authenticated=True, username='owner'),
            url_for=lambda endpoint, **kw: '/static/' + kw['filename'] if endpoint == 'static' else '/' + endpoint.replace('.', '/'),
            HAS_NODES=True, HAS_SETTINGS=True, csrf_token=lambda: 'native-fixture',
        )
        with tempfile.TemporaryDirectory(prefix='commerce-ui-') as directory:
            output = Path(directory)
            with app.test_request_context('/telegram-commerce'):
                (output / 'owner.html').write_text(render_template('telegram-commerce.html', portal_shell_nonce='test-nonce'), encoding='utf-8')
            with app.test_request_context('/telegram-app'):
                (output / 'customer.html').write_text(render_template('telegram-mini-app.html'), encoding='utf-8')
            result = subprocess.run(['node', str(ROOT / 'tests/commerce_ui.cjs'), str(ROOT), str(SHELL), directory],
                                    capture_output=True, text=True, encoding='utf-8', timeout=240)
            print(result.stdout)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_browser_with_real_auth_domain_and_http_contracts(self):
        sys.path.insert(0, str(ROOT))
        from portal_commerce import install_commerce
        from portal_commerce_auth import Auth
        from portal_commerce_service import CommerceService
        from portal_commerce_store import Store
        import io
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (12, 12), 'white').save(image, format='PNG')
        class Native:
            def __init__(self, *args): self.rows = {}
            def targets(self): return [{'scope':'local', 'iface_id':1, 'available':100}]
            def provision(self, operation, spec):
                row = {'id':101,'status':'active','used_bytes':0,'remaining_bytes':30*1073741824,
                       'remaining_seconds':30*86400,'expires_at':int(time.time())+30*86400,
                       'peers':[{'id':7,'name':'دستگاه آزمایشی','public_key':'test-public-key'}]}
                self.rows[operation] = row
                return row
            def subscription(self, native_id): return next(item for item in self.rows.values() if item['id']==native_id)
            def reconcile(self, operation): return self.rows.get(operation)
            def artifact(self, native_id, peer_id, kind):
                return (image.getvalue(), 'image/png') if kind=='qr' else (b'[Interface]\n# private test fixture\n', 'text/plain')
        with tempfile.TemporaryDirectory(prefix='commerce-contract-') as directory:
            app = Flask('commerce-ui-contract', instance_path=directory, template_folder=str(ROOT/'templates'), static_folder=None)
            app.config.update(TESTING=True,SECRET_KEY='temporary-ui-contract',COMMERCE_STORE_FACTORY=Store,
                COMMERCE_SERVICE_FACTORY=CommerceService,COMMERCE_AUTH_FACTORY=Auth,COMMERCE_NATIVE_FACTORY=Native,
                COMMERCE_OWNER_CHECK=lambda:'test-owner')
            app.jinja_loader = ChoiceLoader([FileSystemLoader(ROOT/'templates'),FileSystemLoader(SHELL/'templates')])
            app.jinja_env.globals.update(current_user=SimpleNamespace(is_authenticated=True,username='test-owner'),
                url_for=lambda endpoint,**kw:'/static/'+kw['filename'] if endpoint=='static' else '/'+endpoint.replace('.','/'),
                HAS_NODES=True,HAS_SETTINGS=True,csrf_token=lambda:'native-ui-contract')
            @app.get('/static/<path:filename>')
            def fixture_static(filename):
                for source in (ROOT,SHELL,Path(os.environ.get('OPERATIONS_SUPPORT_ROOT',ROOT))):
                    candidate=(source/'static'/filename).resolve()
                    if candidate.is_relative_to((source/'static').resolve()) and candidate.is_file(): return send_file(candidate)
                abort(404)
            install_commerce(app,None)
            service=app.extensions['portal_commerce_service']
            token='123456:abcdefghijklmnopqrstuvwxyz_fixture_123'
            app.extensions['portal_commerce_runtime_config'].save({'bot_token':token,'public_base_url':'https://example.invalid','bot_enabled':False},'test-owner')
            service.admin_action('test-owner','save_product',{'name':'طرح واقعی آزمایشی','category':'economy','traffic_gb':30,'days':30,'devices':1,'price_toman':200000,'active':True,'target':{'scope':'local','iface_id':1}})
            service.admin_action('test-owner','save_destination',{'card_number':'1111222233334444','cardholder_name':'صاحب حساب آزمایشی','active':True})
            customer=service.customer_from_telegram({'id':12345,'first_name':'مشتری قرارداد'})
            fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':12345,'first_name':'مشتری قرارداد'})}
            secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
            fields['hash']=hmac.new(secret,'\n'.join(key+'='+fields[key] for key in sorted(fields)).encode(),hashlib.sha256).hexdigest()
            fixture=Path(directory)/'browser.json'
            fixture.write_text(json.dumps({'init_data':urlencode(fields)}),encoding='utf-8')
            @app.after_request
            def process_test_jobs(response):
                if request.path.endswith('/review') and response.status_code==200: service.process_jobs()
                return response
            server=make_server('127.0.0.1',0,app,threaded=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                result=subprocess.run(['node',str(ROOT/'tests/commerce_ui.cjs'),'--live',
                    'http://127.0.0.1:'+str(server.server_port),str(fixture)],
                    capture_output=True,text=True,encoding='utf-8',timeout=180)
                print(result.stdout)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
