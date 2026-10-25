"""Read-only native patch preparation plus temporary-file atomic-write checks."""
import ast
from collections import Counter
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

PRODUCT = Path(__file__).resolve().parents[1]
REFERENCE = Path(__file__).resolve().parents[3] / 'upstream-wg-panel'
spec = importlib.util.spec_from_file_location('product_patches', PRODUCT / 'deploy/linux/apply_upstream_patches.py')
patches = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patches)


class NativePatchChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (REFERENCE / '.git').exists():
            raise unittest.SkipTest('Pinned read-only reference checkout is not present')
        cls.changes, cls.originals = patches.prepare(REFERENCE)

    def test_core_and_new_guard_parse(self):
        for name in ('app.py', 'portal_peer_guard.py', 'portal_security.py'):
            ast.parse(self.changes[name])
        self.assertIn('install_peer_guard(app, csrf, globals())', self.changes['app.py'])
        self.assertIn("'public_key': peer.public_key", self.changes['app.py'])
        self.assertIn("'iface_id': peer.iface_id", self.changes['app.py'])

    def test_templates_preserve_native_operations_and_untouched_pages(self):
        class Contracts(HTMLParser):
            def __init__(self):
                super().__init__()
                self.elements = Counter()

            def handle_starttag(self, tag, attrs):
                protected = {'id', 'name', 'href', 'src', 'action', 'method',
                             'type', 'for', 'aria-controls'}
                bindings = tuple(sorted(
                    (key, value or '') for key, value in attrs
                    if key in protected or (key.startswith('data-')
                                             and not key.startswith('data-portal-'))
                ))
                if bindings:
                    self.elements[(tag, bindings)] += 1

        for name, content in self.changes.items():
            if not name.startswith('templates/'):
                continue
            restored = content.replace('<link rel="stylesheet" href="/static/css/portal-theme.css">', '')
            restored = restored.replace('<script defer src="/static/js/portal-locale.js"></script>', '')
            if name == 'templates/dashboard-modern.html':
                native, themed = Contracts(), Contracts()
                native.feed(self.originals[name])
                themed.feed(restored)
                for contract, count in native.elements.items():
                    self.assertGreaterEqual(themed.elements[contract], count,
                                            (name, contract))
                native_ids = [value for (_, bindings), count in themed.elements.items()
                              for key, value in bindings if key == 'id'
                              for _ in range(count)]
                self.assertEqual(len(native_ids), len(set(native_ids)),
                                 'Dashboard IDs must remain unique')
            else:
                self.assertEqual(restored, self.originals[name], name)

    def test_local_font_and_license_are_in_payload(self):
        font = self.changes['static/fonts/portal-vazirmatn.woff2']
        self.assertTrue(font.startswith(b'wOF2'))
        self.assertIn(b'SIL OPEN FONT LICENSE', self.changes['static/fonts/portal-vazirmatn-OFL.txt'])

    def test_atomic_binary_and_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            patches.atomic_write(root / 'font.woff2', b'wOF2\x00\xff')
            patches.atomic_write(root / 'test.txt', 'متن\n')
            self.assertEqual((root / 'font.woff2').read_bytes(), b'wOF2\x00\xff')
            self.assertEqual((root / 'test.txt').read_text(encoding='utf-8'), 'متن\n')

    def test_unknown_anchor_is_refused(self):
        with self.assertRaises(ValueError):
            patches.replace_once('same same', 'same', 'changed')


if __name__ == '__main__':
    unittest.main()
