"""Pure JavaScript transport stub; no network requests."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]

class CsrfHeaderChecks(unittest.TestCase):
    def test_explicit_session_headers_and_native_cookie_fallback(self):
        if not shutil.which('node'):
            self.skipTest('Existing Node runtime required')
        result=subprocess.run(['node',str(ROOT/'tests/csrf_headers.cjs'),str(ROOT)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
