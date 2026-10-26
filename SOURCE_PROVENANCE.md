# Source provenance and product changes

- Base repository: https://github.com/Azumi67/WG_Panel
- Base revision: f0f41068e1fd453d17acad23b9b90bc2a284bf59
- Product repository: https://github.com/ashkanfthy97-alt/WG-Panel-Reseller
- Original Apache-2.0 LICENSE and README.upstream.md retained.
- All 198 tracked files of the pinned upstream source remain present.

Product changes:

1. Navy/cyan Persian RTL and English LTR skin on native operational templates. Native dashboard JavaScript, DOM/API contracts, forms and capabilities remain.
2. A scoped reseller tab registered directly into the native Flask app by portal_reseller_native.py. Existing account/ownership/expiry logic and assets in deploy/linux are reused. Native operations dispatch through bounded authenticated in-process WSGI requests with separate application contexts, with no gateway or extra service required.
3. Independent reseller cookies and signing salt, custom CSRF/origin checks, native administrator verification, resource identity checks and prepaid renewal semantics. No implicit deletion on expiry.
4. Existing security corrections retained: required application secret, protected owner registration, CSRF, restricted Node destinations/transport and quota-preserving peer actions.
5. Original wg.sh bootstrap and wg.py terminal menu/setup/service/WireGuard/Telegram/update/uninstall workflows restored. Root install.sh and install-gui.sh open this native terminal menu. Fork-specific source URL/origin guards preserve customizations; token generation supports the registration guard, and native update exclusions preserve legacy runtime data.
6. Old deploy/linux managed gateway/setup files are retained only for existing work/data compatibility. Default installation no longer dispatches to them. Existing managed services are never stopped or migrated implicitly.
7. Documentation describes private-repository SSH authentication, native install/menu flow and the need to inspect any existing managed VPS before transitioning it.

No real server/data migration or native VPS installation has been performed by this change. The preserved local upstream reference checkout remains untouched.
