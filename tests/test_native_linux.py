"""Native terminal workflow and HTTP-compatible reseller UI checks; no installs."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


class NativeLinuxChecks(unittest.TestCase):
    def function(self, name, **values):
        tree = ast.parse((ROOT / 'wg.py').read_text(encoding='utf-8'))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
        namespace = dict(Path=Path, clear=Mock(), header=Mock(), pause=Mock(),
                         c=lambda value, *_: value, box=lambda title, lines, **_: '\n'.join(lines),
                         hr=lambda *_: '', BR_WHT='', BOLD='', BR_CYN='', BR_BLU='', BR_GRN='',
                         BR_YEL='', BR_RED='', DIM='', render_status=lambda _: '',
                         info=Mock(), ok=Mock(), warn=Mock(), confirm=Mock(return_value=True))
        namespace.update(values)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'native-wg.py', 'exec'), namespace)
        return namespace[name]

    def test_original_menu_opens_native_install_menu(self):
        install = Mock()
        function = self.function('main_menu', get_project=lambda: ROOT,
                                 _menu_input=Mock(side_effect=['2', '0']), install_menu=install)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            function()
        install.assert_called_once_with()
        self.assertIn('WG Panel Control', output.getvalue())
        self.assertIn('Install / Setup', output.getvalue())

    def test_guided_install_uses_native_steps_in_order(self):
        calls = []
        values = {name: Mock(side_effect=lambda *_, _name=name: calls.append(_name)) for name in
                  ('install_requirements', '_venv_requirements', 'env_setup', 'wireguard_setup',
                   'panel_settings', 'runtime', '_service_option', 'telegram_settings', 'telegram_option')}
        values.update(get_project=lambda: ROOT, clone_repo=Mock(return_value=True))
        with contextlib.redirect_stdout(io.StringIO()):
            self.function('install_everything', **values)()
        self.assertEqual(calls, ['install_requirements', '_venv_requirements', 'env_setup',
                                'wireguard_setup', 'panel_settings', 'runtime', '_service_option',
                                'telegram_settings', 'telegram_option'])
        values['clone_repo'].assert_called_once_with()

    def test_unrelated_origin_is_rejected_without_pull(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='https://github.com/Azumi67/WG_Panel.git\n'))
            check = self.function('_product_remote', subprocess=SimpleNamespace(run=runner), err=Mock())
            self.assertFalse(check(root))
            self.assertEqual(runner.call_count, 1)
            self.assertIn('config', runner.call_args.args[0])
            runner.return_value.stdout = 'git@github.com:ashkanfthy97-alt/WG-Panel-Reseller.git\n'
            self.assertTrue(check(root))

    def test_native_env_wizard_preserves_existing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = 'existing-private-test-token-' * 2
            (root / '.env').write_text('SETUP_TOKEN=' + token + '\n', encoding='utf-8')
            parser = self.function('parse_env', Dict=dict)
            render = self.function('_env_text', Dict=dict, List=list)
            options = dict(Dict=dict, readtext=lambda path: path.read_text(encoding='utf-8'),
                           parse_env=parser, _env_text=render, _paths=str,
                           _gen_flask_secret=lambda: 'f' * 48, _gen_fernet_key=lambda: 'e' * 44,
                           _gen_api_key=lambda: 'a' * 48, secrets=Mock(),
                           ask=lambda label, default=None, **_: default,
                           getpass=SimpleNamespace(getpass=Mock(return_value=''), GetPassWarning=UserWarning),
                           box_text=lambda title, value, **_: value,
                           _write=lambda path, value: path.write_text(value, encoding='utf-8'),
                           open=lambda *_args, **_kwargs: io.StringIO())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.function('env_setup', **options)(root)
            self.assertEqual(parser((root / '.env').read_text(encoding='utf-8'))['SETUP_TOKEN'], token)
            self.assertNotIn(token, output.getvalue())
            options['secrets'].token_urlsafe.assert_not_called()

    def test_launcher_opens_native_terminal_without_running_installer(self):
        bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path(shutil.which('bash') or '/missing')
        if not bash.is_file():
            self.skipTest('Bash unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'wg.sh').write_text('#!/bin/bash\nprintf "NATIVE:%s\\n" "$@"\n', encoding='utf-8')
            for name in ('install.sh', 'install-gui.sh'):
                shutil.copyfile(ROOT / name, root / name)
            for name in ('install.sh', 'install-gui.sh'):
                result = subprocess.run([str(bash), (root / name).as_posix(), '--no-apt'],
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), 'NATIVE:--no-apt')

    def test_receipts_work_on_native_http_without_secure_context_apis(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node unavailable')
        source = (ROOT / 'deploy/linux/reseller_ui/reseller.js').read_text(encoding='utf-8')
        functions = source[source.index('  async function receipt('):source.index('  async function action(')]
        program = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const webcrypto = require('node:crypto').webcrypto;
const saved = new Map();
const context = { pending: new Map(), Uint8Array, TextEncoder, encodeURIComponent,
  crypto: {getRandomValues: webcrypto.getRandomValues.bind(webcrypto)},
  sessionStorage: {getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key)} };
vm.createContext(context);
vm.runInContext(FUNCTIONS + ';globalThis.receipt=receipt;globalThis.finishReceipt=finishReceipt;', context);
(async () => {
  const a = await context.receipt('renew:1', {days:30});
  assert.match(a.key, /^[A-Za-z0-9_-]{16,80}$/);
  assert.equal((await context.receipt('renew:1', {days:30})).key, a.key);
  context.pending.clear();
  assert.equal((await context.receipt('renew:1', {days:30})).key, a.key);
  context.finishReceipt(a);
  assert.notEqual((await context.receipt('renew:1', {days:30})).key, a.key);
})().catch(error => {console.error(error);process.exitCode=1;});
'''.replace('FUNCTIONS', json.dumps(functions))
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'receipt.cjs'
            script.write_text(program, encoding='utf-8')
            result = subprocess.run([node, str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
