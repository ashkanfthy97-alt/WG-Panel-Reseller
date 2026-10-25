# Source provenance and product changes

- Base repository: https://github.com/Azumi67/WG_Panel
- Base revision: f0f41068e1fd453d17acad23b9b90bc2a284bf59
- Product repository: https://github.com/ashkanfthy97-alt/WG-Panel-Reseller
- License: original Apache-2.0 LICENSE retained; original README retained as README.upstream.md.
- This distribution contains all 198 files tracked by the pinned upstream revision, with product changes materialized directly in the native source tree.

Product changes:

1. Navy/cyan Persian RTL and English LTR skin, native dashboard template and locale support. Native dashboard JavaScript and its DOM/API contracts are retained.
2. Scoped reseller extension in deploy/linux, with independent sessions, resource ownership, expiry and renewal. The gateway routes native requests to the actual Flask application.
3. Existing security changes preserved: required application secret, protected first-run owner registration, CSRF, restricted Node destinations and transport, peer identity checks, and protected upstream updater.
4. Installation runs the packaged repository root. Local launchers and native guided setup dispatch to the managed installer; upstream clone/update cannot silently replace product code.
5. Example systemd paths corrected for /opt/wg-panel; installer generates actual paths. No build-machine path, runtime secret, database or nested Git checkout is included.
6. Prior standalone demo dist/screens are not part of the replacement tree. Installation and navigation use operational native pages.

No data migration from the previous vendor layout is implied. The preserved local upstream reference checkout remains unchanged.
