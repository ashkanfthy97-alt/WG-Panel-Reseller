"""Source-release checks; no installer or real service is executed."""
import ast
from collections import Counter
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/linux'


class NativeSourceChecks(unittest.TestCase):
    def test_native_sources_and_license_are_present(self):
        for name in ('app.py', 'models.py', 'auth.py', 'forms.py', 'wg.py',
                     'telegram_bot.py', 'telegram_admin.py', 'agent/node_agent.py',
                     'panel_core/node_client.py', 'templates/index.html',
                     'static/js/dashboard-modern.js', 'LICENSE', 'README.upstream.md'):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertEqual((ROOT / 'UPSTREAM_REVISION').read_text().strip(),
                         'f0f41068e1fd453d17acad23b9b90bc2a284bf59')
        self.assertFalse((ROOT / 'dist').exists())

    def test_python_sources_parse(self):
        for source in ROOT.rglob('*.py'):
            if any(part in ('venv', '__pycache__', 'runtime') for part in source.parts):
                continue
            ast.parse(source.read_text(encoding='utf-8-sig'), filename=str(source))

    def test_installer_runs_packaged_source_without_fetching_another_core(self):
        installer = (DEPLOY / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('VENDOR="$SITE"', installer)
        self.assertIn('UPSTREAM_REVISION', installer)
        self.assertIn('"$VENDOR/app.py"', installer)
        for forbidden in ('git clone', 'git -C "$VENDOR" checkout',
                          'apply_upstream_patches.py', '$SITE/dist/', 'vendor/WG_Panel'):
            self.assertNotIn(forbidden, installer)

    def test_launchers_and_service_examples_are_portable(self):
        for name in ('install-gui.sh', 'wg.sh'):
            text = (ROOT / name).read_text(encoding='utf-8')
            self.assertIn('BASH_SOURCE[0]', text)
            self.assertNotIn('raw.githubusercontent.com', text)
        for name in ('systemd/bot.service', 'systemd/wg-node-agent.service'):
            text = (ROOT / name).read_text(encoding='utf-8')
            self.assertIn('/opt/wg-panel', text)
            for forbidden in ('C:\\Users\\', '/root/wp2', '/root/wg/'):
                self.assertNotIn(forbidden, text)

    def _native_function(self, name, **namespace):
        source = ast.parse((ROOT / 'wg.py').read_text(encoding='utf-8'))
        function = next(node for node in source.body
                        if isinstance(node, ast.FunctionDef) and node.name == name)
        module = ast.Module(body=[function], type_ignores=[])
        exec(compile(module, 'wg.py', 'exec'), namespace)
        return namespace[name]

    def test_native_clone_and_setup_cannot_replace_product_source(self):
        self.assertFalse(self._native_function('clone_repo', warn=Mock(), pause=Mock())())
        for name in ('install_everything', '_quick_setup'):
            managed = Mock()
            self._native_function(name, _managed_install=managed)()
            managed.assert_called_once_with()
        self._native_function('update_menu', warn=Mock(), pause=Mock())()

    def test_native_dashboard_keeps_script_and_interaction_contracts(self):
        class Bindings(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = Counter()

            def handle_starttag(self, tag, attrs):
                keep = {'id', 'href', 'src', 'name', 'type', 'aria-controls'}
                value = tuple(sorted((key, value or '') for key, value in attrs
                                     if key in keep or (key.startswith('data-')
                                                         and not key.startswith('data-portal-'))))
                if value:
                    self.values[(tag, value)] += 1

        source = (ROOT / 'templates/dashboard-modern.html').read_text(encoding='utf-8')
        self.assertIn("filename='js/dashboard-modern.js'", source)
        for name in ('nx-dashboard', 'nx-refresh', 'nx-traffic-chart',
                     'nx-system-toggle', 'nx-recent-peers'):
            self.assertIn('id="' + name + '"', source)
        reference = ROOT.parent.parent / 'upstream-wg-panel/templates/dashboard-modern.html'
        if reference.is_file():
            native, packaged = Bindings(), Bindings()
            native.feed(reference.read_text(encoding='utf-8'))
            packaged.feed(source)
            for binding, count in native.values.items():
                self.assertGreaterEqual(packaged.values[binding], count, binding)

    def test_telegram_service_ownership_uses_packaged_root(self):
        spec = importlib.util.spec_from_file_location('source_tls_helper', DEPLOY / 'service_helpers/tls_service.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / 'package'
            deployment = package / 'deploy/linux'
            deployment.mkdir(parents=True)
            unit = Path(temporary) / 'bot.service'
            unit.write_text('WorkingDirectory=' + str(package) + '\n', encoding='utf-8')
            with patch.object(helper.subprocess, 'run') as run, patch.object(helper, 'assert_secure_ancestry'):
                helper.start_telegram(deployment, unit)
                run.assert_called_once()
            unit.write_text('WorkingDirectory=/another/installation\n', encoding='utf-8')
            with patch.object(helper.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    helper.start_telegram(deployment, unit)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
