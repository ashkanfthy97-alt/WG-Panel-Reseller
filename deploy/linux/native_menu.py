"""Open the pinned WG_Panel CLI in its own project directory.

runpy avoids its __main__ wrapper installation, so an existing global
wgpanel command belonging to another installation is never replaced.
"""

import os
import runpy
import sys
from pathlib import Path


vendor = Path(sys.argv[1]).resolve()
if not (vendor / "app.py").is_file() or not (vendor / "wg.py").is_file():
    raise SystemExit("WG_Panel source is missing")
os.chdir(vendor)
cli = runpy.run_path(str(vendor / "wg.py"), run_name="wgportal_embedded")
action = sys.argv[2] if len(sys.argv) > 2 else "menu"
if action == "wireguard":
    cli["wireguard_setup"](vendor)
elif action == "telegram":
    cli["telegram_setup"]()
elif action == "menu":
    cli["main_menu"]()
else:
    raise SystemExit("Unknown management action")
