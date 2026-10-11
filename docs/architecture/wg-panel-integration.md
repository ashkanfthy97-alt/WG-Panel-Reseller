# WG_Panel integration map

The Linux package runs Azumi67/WG_Panel at commit
`f0f41068e1fd453d17acad23b9b90bc2a284bf59` as its operational core.
The upstream Apache-2.0 source is fetched into
`deploy/linux/vendor/WG_Panel` during installation. It is not rewritten
or represented as a mock backend.

## Upstream code paths used

| Upstream path | Role in the running product |
| --- | --- |
| `app.py` | Flask routes, startup/bootstrap, WireGuard operations, API and Gunicorn launch |
| `auth.py`, `models.py`, `config.py` | Authentication, database records and configuration |
| `panel_core/`, `panel_operations/` | Profiles, node communication, health, security and peer operations |
| `agent/` | Remote-node installation and API |
| `telegram_bot.py`, `telegram_admin.py` | Telegram commands and notifications |
| `templates/`, `static/` | Complete native operational UI, served on routes such as `/users` |
| `wg.py`, `wg.sh` | Guided native installation and management menus |

## Project runtime

- `install.sh` installs the same Debian/Ubuntu system packages and Python
  virtual environment pattern as upstream `wg.sh`/`wg.py`. It writes the
  upstream `.env`, `instance/panel_settings.json` and
  `instance/runtime.json` where upstream expects them.
- `wg-panel.service` launches the pinned upstream `app.py` on
  `127.0.0.1:8766`. `wg-panel-bot.service` is optional.
- `wg-portal.service` serves the custom bilingual frontend on
  `127.0.0.1:8765` and forwards operational routes to WG_Panel. It does
  not bind public port 443.
- `wgportal` opens the project install/status/log menu. The upstream
  menu is available inside it; `wgpanel` is added only when that command
  does not already belong to another installation. The WireGuard `wg`
  binary is never replaced.

## Current feature boundary

The upstream `/users` UI remains the complete management interface for
peers, interfaces, subscriptions, nodes, backups, logs, settings and
Telegram configuration. The custom `/app.html` view currently reads
live overview and peer data after WG_Panel authentication. Reseller
accounts, credit, payments and order provisioning in that custom view
are still preview-only and require separate backend work. Updating the
upstream source through its own menu changes the pinned revision; review
and repin it before rerunning the project installer.
