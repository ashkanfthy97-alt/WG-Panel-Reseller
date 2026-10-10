# WG Portal feature inventory

This inventory compares the public WG_Panel feature list with the current bilingual Site prototype. “UI model” means the idea is represented in the demo interface; it does not mean the operation is connected or production-ready.

## Shared platform

| Area | WG_Panel capability | Current Site status |
|---|---|---|
| Dashboard | CPU, memory, disk, network, peers, health, endpoint and system overview | UI model with sample charts and data |
| Peer management | Create/edit/enable/disable/delete, bulk create, reset usage/timer, QR/config/link/logs | UI model; actions remain non-operational |
| Interfaces | Create and manage interfaces, DNS, MTU, listen port, forwarding/NAT, endpoint | Navigation and interface controls modeled; no host access |
| Subscriptions | Multi-location configs, existing/new peers, data/time limits, unlimited, first-use timer | Plans and subscription studio modeled; no provisioning |
| Client delivery | Public status/usage page, QR, config download, short links, support | Delivery and customer areas modeled; no real links/configs |
| Profiles | Reusable peer/subscription settings and defaults | Navigation and profile controls modeled |
| Nodes | Remote nodes, connection state, search, refresh, test and peer/interface management | Node list and health UI modeled; no remote API |
| Operations | Logs, diagnostics, backups, restore, scheduled backup | Sections modeled; no host or database operations |
| Telegram | Admin bot, peer actions, configs/QR, profiles, backup, notifications, multiple admins | Bot setup/command area modeled; not connected |
| Security | Admin login, TOTP, HTTPS settings, traffic control, HTTP monitor and temporary blocks | Security settings and policies modeled; enforcement absent |
| Configuration | Runtime, bot, interface, domain/endpoint and API settings/docs | Settings and API docs modeled; configuration is illustrative |

## Business model additions

| Role | Intended behavior | Current Site status |
|---|---|---|
| Mother admin | Approve partners, manage nodes/plans, issue reseller panels, prices, credits, orders, policies and audit | Admin navigation and demo role selector |
| Full-panel reseller | Own customers and peers; use a complete scoped panel; prepay/recharge monthly; panel pauses on expiry and resumes after renewal | Reseller workspace and usage/credit UI modeled; lifecycle requires backend jobs |
| Order-only partner | Submit customer-specific orders and buy fixed plans without a panel; approved high-volume tiers can use wholesale pricing | Partner order flow and sample plans modeled; no payment/provisioning |
| Direct customer | Receive a delivery page/config through a partner or an explicitly enabled direct flow | Customer delivery UI modeled; no config generation |

## Before production

Implement these as connected server capabilities before accepting real orders: tenant schema and migrations, server-enforced role/object authorization, idempotent wallet ledger and payments, quota/expiry jobs, concurrency-safe WireGuard provisioning, audited reseller suspension/resumption, Persian Telegram identity binding and scoped commands, secure customer delivery tokens, n8n webhooks with signatures and replay protection, tested backups, monitoring, rate limits, and an independent security review. The static Site must never store WireGuard private keys, panel API keys, bot tokens, payment secrets, or real customer records.
