"""Optional real-browser regression for locale timing and reseller login UI."""
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


class InitialUiChecks(unittest.TestCase):
    def test_first_paint_and_reseller_login_in_browser(self):
        if not os.environ.get('PLAYWRIGHT_MODULE_PATH') or not shutil.which('node'):
            self.skipTest('Set PLAYWRIGHT_MODULE_PATH to an existing Playwright installation')
        app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        app.secret_key = 'temporary-render-secret'
        def url(endpoint, **values):
            if endpoint == 'static':
                return '/static/' + values['filename']
            return {'index': '/', 'login': '/login', 'users': '/users'}.get(endpoint, '/' + endpoint.replace('.', '/'))
        app.jinja_env.globals['url_for'] = url
        class Field:
            data = ''
            def __init__(self, name):
                self.name = name
                self.label = Markup('<span>') + escape(name.replace('_', ' ').title()) + Markup('</span>')
            def __call__(self, **attrs):
                attrs.setdefault('name', self.name)
                attrs.setdefault('id', self.name)
                attrs.setdefault('type', 'checkbox' if self.name in ('unlimited', 'start_on_first_use') else 'text')
                return Markup('<input ') + Markup(' ').join(escape(key) + Markup('="') + escape(value) + Markup('"') for key, value in attrs.items()) + Markup('>')
        class Form:
            def hidden_tag(self):
                return Markup('<input type="hidden" name="csrf_token" value="fixture">')
            def __getattr__(self, name):
                return Field(name)
        with tempfile.TemporaryDirectory() as directory, app.test_request_context('/'):
            target = Path(directory)
            values = dict(current_user=SimpleNamespace(is_authenticated=True, is_admin=True,
                is_superuser=True, username='mother'), HAS_NODES=True, HAS_SETTINGS=True,
                csrf_token=lambda: 'temporary-csrf', PANEL_TIMEZONE='UTC', form=Form())
            for name in ('dashboard-modern.html', 'login.html', 'users.html'):
                (target / name).write_text(render_template(name, **values), encoding='utf-8')
            result = subprocess.run(['node', str(ROOT / 'tests/portal_initial_ui.cjs'), str(ROOT), str(target)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
