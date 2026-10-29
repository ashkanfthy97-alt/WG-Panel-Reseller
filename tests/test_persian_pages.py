"""Optional isolated native-template Persian browser smoke; no application import."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from flask import Flask, render_template
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parents[1]
PAGES = ('users', 'subscriptions', 'nodes', 'operations', 'logs', 'backup',
         'settings', 'api_docs', 'login', 'register', 'user_peer',
         'user_peer_minimal', 'user_peer_compact', 'user_peer_pro', 'subscription_public')


class PersianPagesChecks(unittest.TestCase):
    def test_rendered_native_pages(self):
        if not os.environ.get('PLAYWRIGHT_MODULE_PATH') or not shutil.which('node'):
            self.skipTest('Existing Node and PLAYWRIGHT_MODULE_PATH required')
        app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        app.secret_key = 'isolated-template-fixture'
        app.jinja_env.globals['url_for'] = lambda endpoint, **kw: (
            '/static/' + kw['filename'] if endpoint == 'static' else '/' + endpoint.replace('.', '/'))

        class Field:
            data = ''
            errors = ()
            choices = ()
            def __init__(self, name):
                self.name = name
                self.label = Markup('<span>') + escape(name.replace('_', ' ').title()) + Markup('</span>')
            def __call__(self, **attrs):
                attrs.setdefault('name', self.name)
                attrs.setdefault('id', self.name)
                attrs.setdefault('type', 'text')
                return Markup('<input ') + Markup(' ').join(
                    escape(k) + Markup('="') + escape(v) + Markup('"') for k, v in attrs.items()) + Markup('>')
        class Form:
            def hidden_tag(self):
                return Markup('<input type="hidden" name="csrf_token" value="fixture">')
            def __getattr__(self, name):
                return Field(name)

        with tempfile.TemporaryDirectory(prefix='persian-pages-') as directory, app.test_request_context('/'):
            target = Path(directory)
            values = dict(current_user=SimpleNamespace(is_authenticated=True, is_admin=True,
                is_superuser=True, username='Settings'), HAS_NODES=True, HAS_SETTINGS=True,
                csrf_token=lambda: 'fixture-csrf', PANEL_TIMEZONE='UTC', form=Form(), token='fixture-token',
                sub=SimpleNamespace(name='Settings', token='fixture-token', id=1), data={})
            for page in PAGES:
                (target / (page + '.html')).write_text(render_template(page + '.html', **values), encoding='utf-8')
            (target / 'subscription_public_custom.html').write_text(render_template('subscription_public.html',
                **values, portal_settings={'usage_title': 'Settings', 'install_title': 'Online',
                    'configs_title': 'Custom customer configs', 'support_title': 'Customer support name'}), encoding='utf-8')
            result = subprocess.run(['node', str(ROOT / 'tests/persian_pages_ui.cjs'), str(ROOT), str(target)],
                capture_output=True, text=True, encoding='utf-8', timeout=240)
            print(result.stdout)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
