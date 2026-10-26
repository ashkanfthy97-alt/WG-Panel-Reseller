"""Quick-install checks with temporary files and a harmless launcher stub."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/linux'
sys.path.insert(0, str(DEPLOY))
spec = importlib.util.spec_from_file_location('quick_bootstrap', DEPLOY / 'bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class QuickTokenChecks(unittest.TestCase):
    def setUp(self):
        try:
            import dotenv  # noqa: F401
        except ImportError:
            # The review runtime has no dotenv. Exercise real token/file logic
            # with the existing parser for these simple, non-interpolated fixtures.
            from install_helpers import _env_values
            def fixture_env(path):
                source = path.read_text(encoding='utf-8') if path.is_file() else ''
                return {key: values[-1] for key, values in _env_values(source).items()}
            reader = patch.object(bootstrap, 'load_env', side_effect=fixture_env)
            reader.start()
            self.addCleanup(reader.stop)

    def test_quick_mode_generates_without_prompt_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text('# keep\nOTHER=preserved\n', encoding='utf-8')
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(bootstrap.getpass, 'getpass', side_effect=AssertionError('must not prompt')), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                token = bootstrap.ensure_token(env, generate=True)
            self.assertGreaterEqual(len(token), 32)
            self.assertLessEqual(len(token), 256)
            self.assertEqual(bootstrap.load_env(env)['SETUP_TOKEN'], token)
            self.assertIn('# keep\nOTHER=preserved\n', env.read_text(encoding='utf-8'))
            self.assertEqual(output.getvalue() + errors.getvalue(), '')
            if os.name != 'nt':
                self.assertEqual(env.stat().st_mode & 0o077, 0)

    def test_repeated_quick_mode_preserves_token_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            original = 'SETUP_TOKEN=' + 'a' * 48 + '\nUNCHANGED=yes\n'
            env.write_text(original, encoding='utf-8')
            with patch.object(bootstrap.secrets, 'token_urlsafe', side_effect=AssertionError('must preserve existing token')):
                self.assertEqual(bootstrap.ensure_token(env, generate=True), 'a' * 48)
            self.assertEqual(env.read_text(encoding='utf-8'), original)

    def test_invalid_existing_token_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text('SETUP_TOKEN=short\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                bootstrap.ensure_token(env, generate=True)
            self.assertEqual(env.read_text(encoding='utf-8'), 'SETUP_TOKEN=short\n')

    def test_manual_mode_still_uses_hidden_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            prompt = Mock(return_value='m' * 48)
            self.assertEqual(bootstrap.ensure_token(env, prompt), 'm' * 48)
            prompt.assert_called_once_with()

    def test_display_writes_only_to_selected_terminal(self):
        terminal, stdout, stderr = io.StringIO(), io.StringIO(), io.StringIO()
        token = 'private-test-token-' * 3
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text('SETUP_TOKEN=' + token + '\n', encoding='utf-8')
            before = env.read_bytes()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                bootstrap.show_setup_token(env, terminal=terminal)
            self.assertEqual(env.read_bytes(), before)
        self.assertIn(token, terminal.getvalue())
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())

    def test_display_without_tty_does_not_leak_token(self):
        token, stdout, stderr = 'private-test-token-' * 3, io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text('SETUP_TOKEN=' + token + '\n', encoding='utf-8')
            with patch.object(bootstrap, 'open', side_effect=OSError('no controlling terminal'), create=True), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertFalse(bootstrap.show_setup_token(env))
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())

    def test_missing_token_display_does_not_create_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            with self.assertRaises(ValueError):
                bootstrap.show_setup_token(env, terminal=io.StringIO())
            self.assertFalse(env.exists())


class QuickLauncherChecks(unittest.TestCase):
    def test_foreign_service_is_rejected_before_prerequisite_packages(self):
        bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path(shutil.which('bash') or '/missing/bash')
        if not bash.is_file():
            self.skipTest('Bash is not installed in the review environment')
        source = (DEPLOY / 'install.sh').read_text(encoding='utf-8')
        # Exercise only the preflight with shell stubs. apt/systemctl can never
        # reach their real executables from this isolated script.
        preflight = source[source.index('VENDOR="$SITE"'):source.index('BOT_ENABLED=0 BOT_ACTIVE=0')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit, log = root / 'foreign.service', root / 'apt.log'
            unit.write_text('WorkingDirectory=/another/installation\n', encoding='utf-8')
            script = '\n'.join([
                'set -euo pipefail', 'QUICK=1; MODE=managed; PANEL_PORT=8766; PORTAL_PORT=8765; SETUP_PORT=8767',
                'SITE=' + shlex.quote(root.as_posix()), 'ROOT="$SITE/deploy/linux"',
                'TEST_UNIT=' + shlex.quote(unit.as_posix()), 'APT_LOG=' + shlex.quote(log.as_posix()),
                'command() { if [[ "$1" == -v && "$2" == ss ]]; then return 1; fi; builtin command "$@"; }',
                'apt-get() { printf "apt\\n" >> "$APT_LOG"; }',
                'systemctl() { printf "%s\\n" "$TEST_UNIT"; }', preflight,
            ])
            temporary_script = root / 'preflight.sh'
            temporary_script.write_text(script, encoding='utf-8')
            result = subprocess.run([str(bash), temporary_script.as_posix()], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists(), 'A foreign installation must be rejected before any apt invocation')
            self.assertIn('belongs to another installation', result.stdout + result.stderr)

    def test_root_entrypoints_forward_to_installer_without_running_it(self):
        bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path(shutil.which('bash') or '/missing/bash')
        if not bash.is_file():
            self.skipTest('Bash is not installed in the review environment')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / 'deploy/linux'
            deployment.mkdir(parents=True)
            # Only this stub is invoked; it cannot install packages or services.
            (deployment / 'install.sh').write_text('#!/bin/bash\nprintf "ARG:%s\\n" "$@"\n', encoding='utf-8')
            for name in ('install.sh', 'install-gui.sh'):
                shutil.copyfile(ROOT / name, root / name)
            for name in ('install.sh', 'install-gui.sh'):
                result = subprocess.run([str(bash), (root / name).as_posix(), '--panel-port', '9876'], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.splitlines(), ['ARG:--quick', 'ARG:--panel-port', 'ARG:9876'])

    def test_quick_mode_retains_native_installer_safety_and_readiness(self):
        source = (DEPLOY / 'install.sh').read_text(encoding='utf-8')
        self.assertIn('--quick)', source)
        self.assertIn('--generate-token', source)
        self.assertIn('--show-token', source)
        self.assertLess(source.index('Local TLS setup readiness failed'), source.rindex('--show-token'))
        self.assertIn('listener_owned', source)
        self.assertIn('assert_secure_checkout', source)
        self.assertNotIn('git reset', source)


if __name__ == '__main__':
    unittest.main()
