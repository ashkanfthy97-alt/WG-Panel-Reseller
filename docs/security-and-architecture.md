# Architecture and security notes

## Evidence and scope

The public WG_Panel repository was inspected as a feature and architecture reference. The live panel could not be accessed reliably from this environment. These notes are design requirements and static observations, not a complete penetration test or a claim that the upstream project is vulnerable.

The reference has a broad Flask control plane, SQLAlchemy models for peers/subscriptions/nodes/admin accounts, browser UI routes, API-key-protected endpoints, WireGuard host operations, and a separate Telegram bot process. It currently models one admin account and has no reseller tenant, customer ownership, monthly credit ledger, or one-off commerce model. Its API key is a broad integration credential; it must not be exposed in a browser or reused as a reseller credential.

## Target topology

1. Static Sites frontend for public pages and authenticated portal UI.
2. A separately hosted application API for identity, tenancy, products, orders, balances, audit, and notifications.
3. A narrow WireGuard adapter on the existing VPS, responsible only for provisioning/reading scoped panel resources.
4. A separate Persian Telegram bot process using the application API, with role-aware commands.
5. n8n connects through authenticated HTTPS APIs/webhooks. No direct database access and no shared admin API key.

The Sites runtime is not the WireGuard host. No VPS listener or firewall rule is changed by this frontend project.

## Roles and mandatory server-side boundaries

- **Mother admin:** all tenants, inventory, price plans, partners, nodes, security, operations, audit, backups, and Telegram administration.
- **Full-panel reseller:** only their own customers/peers, subscriptions, profile defaults, usage, order/recharge history, and reseller-scoped bot commands.
- **Order-only partner:** approved catalog, create and track customer-specific orders, retrieve delivery only for owned orders, and view own receipts.
- **Customer:** only a limited subscription delivery/status page using a revocable, high-entropy token.

Every object read or mutation must be checked against the authenticated tenant/owner on the server. UI role switching is for preview only. Deny by default; do not trust submitted owner IDs, role names, prices, remaining balances, peer IDs, or status fields.

## Security controls to implement before production

- Strong password hashing, MFA for privileged roles, secure session cookies, CSRF protection, login throttling, session rotation/revocation, and audited recovery.
- Short-lived scoped sessions/tokens; secrets only in server-side secret storage. Redact bot tokens, WireGuard private keys, API credentials, and configuration contents from logs and exports.
- Object-level authorization on every endpoint, tenant-aware query scopes, explicit property allowlists, schema validation, and response minimization.
- Idempotency keys and database transactions for order placement, credit adjustments, top-ups, and provisioning. Use an append-only balance ledger; never mutate balance without a matching ledger event.
- Confirm destructive actions; soft-delete or revoke where operationally safe; audit actor, target, before/after, timestamp, and request ID.
- Rate limits and quotas on signup, login, order creation, config/token delivery, bot updates, diagnostics, and expensive node operations.
- Restrict outbound URLs and node endpoints to mitigate SSRF; use TLS verification, short timeouts, response size caps, retry budgets, and circuit breakers.
- Verify Telegram webhook secret headers if webhooks are used; deduplicate by update ID. If the bot uses polling, run a single managed worker and keep its token private. Scope each command to the verified Telegram identity and role.
- Use TLS at the existing ingress, strict host/proxy trust settings, CSP, HSTS, nosniff, frame protection, referrer policy, and dependency/source scanning. Do not treat nonstandard ports as a security boundary.
- Encrypt backups, restrict and audit restore, test restoration, and keep WireGuard private keys and bot/API tokens out of downloadable customer-facing archives.
- Centralize structured logs and metrics with secret redaction; alert on repeated auth failures, cross-tenant denial events, unusual order volume, and provisioning failures.

## Static review priorities for the reference

- `config.py` includes a fallback Flask secret (`change-me`) and secure cookies default off. Deployment must fail closed when required production secrets/TLS settings are missing.
- `models.py` contains only `AdminAccount` as an identity model; peer and subscription rows have no tenant owner. Additive multitenancy migrations and authorization are prerequisites.
- `auth.py` accepts a global API key or the logged-in admin for integration routes. Keep that key server-side and replace this trust model at the product boundary with scoped service credentials.
- `app.py` executes host-level operations and has update/backup/security endpoints. Keep these behind narrow admin-only commands and do not mirror them indiscriminately to reseller APIs.
- `telegram_bot.py` is a separate process and currently exposes extensive administrator workflows. New partner commands need identity binding, per-tenant authorization, replay-safe state changes, and Persian copy.

These are concrete hardening/integration priorities from source inspection, not exploit confirmation. Review actual VPS environment, proxy trust, firewall, dependencies, and live configuration before production.

## API boundary sketch

All routes are versioned under `/api/v1`. Authentication is by secure first-party session for the browser and scoped service credentials for bot/n8n. Every response uses a stable request ID and typed error envelope. Mutating commerce/provisioning operations accept an idempotency key.

- `GET /api/v1/me` — actor identity, role, tenant, capabilities.
- `GET /api/v1/overview` — role-scoped counts and usage summary.
- `GET|POST /api/v1/customers` — reseller-scoped customer directory; create customer.
- `GET|POST /api/v1/subscriptions` — own subscriptions; create a subscription subject to server-enforced plan/credit policy.
- `POST /api/v1/orders` — order-only partner creates an order from a server-owned catalog price.
- `GET /api/v1/orders/{id}` — only owner/admin can read; delivery is returned only after provisioning succeeds.
- `GET /api/v1/ledger` and admin-only adjustment operations — immutable, auditable credit history.
- `POST /api/v1/telegram/webhook` — verify Telegram secret header and deduplicate update IDs, or use isolated long-poll worker.

WireGuard private configuration payloads are delivered only through a dedicated, short-lived, revocable endpoint. The Sites frontend never receives a master panel API key.
