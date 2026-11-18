"""Isolated owner peer-audit UI; all API responses are mocked."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
class OwnerPeerAuditBrowserChecks(unittest.TestCase):
    def test_counts_full_list_filters_errors_and_mobile(self):
        if not shutil.which('node') or not os.environ.get('PLAYWRIGHT_MODULE_PATH'):
            self.skipTest('Existing Node and Playwright runtime required')
        result=subprocess.run(['node',str(ROOT/'tests/owner_peer_audit_ui.cjs'),str(ROOT)],capture_output=True,text=True,encoding='utf-8',timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
