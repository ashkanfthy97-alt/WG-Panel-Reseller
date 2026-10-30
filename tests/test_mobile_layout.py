"""Actual native drawer and published reseller UI; all network responses are fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MobileLayoutChecks(unittest.TestCase):
    def test_native_drawer_and_reseller_mobile_layout(self):
        if not shutil.which('node') or not os.environ.get('PLAYWRIGHT_MODULE_PATH'):
            self.skipTest('Existing Node and Playwright runtime required')
        assets = Path(os.environ.get('MOBILE_ASSET_ROOT', ROOT))
        result = subprocess.run(['node', str(ROOT / 'tests/mobile_layout.cjs'),
                                 str(assets), str(ROOT)],
                                capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
