"""Render the actual native base and reseller route; browser requests stay mocked."""
import importlib.util,json,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from flask import Flask,render_template
from jinja2 import ChoiceLoader,FileSystemLoader
ROOT=Path(__file__).resolve().parents[1]
SUPPORT=Path(os.environ.get('SHELL_SUPPORT_ROOT',ROOT))
sys.path.insert(0,str(SUPPORT));sys.path.insert(0,str(SUPPORT/'deploy/linux'));sys.path.insert(0,str(ROOT/'deploy/linux'))
spec=importlib.util.spec_from_file_location('shell_browser_routes',ROOT/'deploy/linux/portal_reseller.py');routes=importlib.util.module_from_spec(spec);spec.loader.exec_module(routes)
class ShellBrowserChecks(unittest.TestCase):
 def test_shared_native_shell_and_reseller_workflows(self):
  if not shutil.which('node') or not os.environ.get('PLAYWRIGHT_MODULE_PATH'):self.skipTest('Existing browser runtime required')
  with tempfile.TemporaryDirectory(prefix='shell-browser-') as directory:
   app=Flask(__name__,template_folder=str(ROOT/'templates'),instance_path=directory);app.secret_key='mock-shell-secret'
   app.jinja_loader=ChoiceLoader([FileSystemLoader(ROOT/'templates'),FileSystemLoader(SUPPORT/'templates')])
   app.jinja_env.globals.update(url_for=lambda endpoint,**kw:'/static/'+kw['filename'] if endpoint=='static' else '/'+endpoint.replace('.','/'),current_user=SimpleNamespace(is_authenticated=False,username=None),HAS_NODES=True,HAS_SETTINGS=True,csrf_token=lambda:'native-fixture')
   class Core:
    def json(self,method,path,payload=None,**kw):return (200,{'username':'owner'}) if path=='/api/admin' else (200,{})
   database=Path(directory)/'resellers.sqlite3';store=routes.ResellerStore(database)
   account=store.create('dealer','safe-browser-password',[1],10);store.approve(account['id']);account=store.get(account['id'])
   app.register_blueprint(routes.reseller_blueprint('',database,'fake',core=Core(),owner_cookie_reader=lambda:'session=owner',native_scheme=True))
   @app.get('/native')
   def native():return render_template('base.html')
   client=app.test_client()
   out=Path(directory)
   for name in ['native','resellers','reseller']:
    if name=='reseller':
     with client.session_transaction() as session:session['reseller_identity']={'id':account['id'],'version':account['auth_version']}
    response=client.get('/'+name);self.assertEqual(response.status_code,200,response.get_data(as_text=True))
    (out/(name+'.html')).write_bytes(response.get_data());(out/(name+'.headers.json')).write_text(json.dumps({'Content-Security-Policy':response.headers['Content-Security-Policy']} if response.headers.get('Content-Security-Policy') else {}))
   result=subprocess.run(['node',str(ROOT/'tests/shell_workspace_ui.cjs'),str(ROOT),str(SUPPORT),directory],capture_output=True,text=True,encoding='utf-8',timeout=240)
   print(result.stdout);self.assertEqual(result.returncode,0,result.stdout+result.stderr)
if __name__=='__main__':unittest.main()
