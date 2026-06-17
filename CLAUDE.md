# Arc Connect Provider Portal — context for Claude

You are looking at the Arc Connect Provider Portal demo, a working Flask + SQLite prototype built during R&D for ABM Respiratory Care. This file orients you so you can answer questions about the code, extend it, or rebuild parts of it without re-deriving context from scratch. Read this before you start.

This repository is the portal source itself — `app.py`, `templates/`, `static/`, etc. are at the repository root (there is no separate `portal/` subfolder).

## What this is

A multi-tenant web portal for HME and Home Health organizations to manage patients on **BiWaze Cough** (mechanical insufflator-exsufflator) and **BiWaze Clear** (oscillating PEP) respiratory therapy devices. It complements the already-released patient-facing **Arc Connect mobile app** that shares the same database.

Tenant tiers:
- **ABMRC** — manufacturer / super-admin tenant; view-only oversight, BAA management, onboarding.
- **Customer main location** (e.g., "Adapt Respiratory") — an operating location that ALSO rolls up its branch locations. It is a real site (has its own patients/devices/users) *and* the network root — there is no separate location-less "group" tier.
- **Customer branch location** (e.g., "Adapt — Denver") — an operational site that rolls up under a main location.
- **Patient mobile app** — separate codebase already in production; portal exposes a feature-gating endpoint for it.

## Canonical user-facing terminology (don't drift)

| Schema / code term | User-facing copy |
|---|---|
| `organizations.type='parent'` | **Main location** |
| `organizations.type='location'` (with `parent_id`) | **Branch location** (or "branch") |
| `organizations.type='internal'` | **ABMRC** (super-admin tenant) |
| `users.role='admin'` in a parent org | **Group admin** |
| `users.role='admin'` in a location org | **Branch-location admin** |
| Parent rollup view | **Network rollup** |

**Why this matters:** "parent / child" and "headquarters" are jargon that confuses customer-facing screens. The product team explicitly chose **"main location"** and **"branch location"** as the canonical wording (and explicitly rejected "headquarters" and "satellite").

**How to apply:** Use this wording in all user-facing copy (templates, button labels, dropdowns, banners, page titles, help text). **Do not rename schema columns or function identifiers** — `parent_id`, `is_parent_admin()`, `parent_overview` route, `alert_rules_source ∈ {'location','parent'}` etc. all stay; the rename would be an expensive refactor with zero user-visible benefit.

## Location-access model (the main-location / branch model)

A user's location scope is explicit, not implied by `organization_id`:
- `users.all_locations = 1` → **All locations**: every current and future branch under their main location, plus the main location itself. Admin-only. Dynamic (new branches auto-included).
- otherwise → the explicit set in **`user_location_access(user_id, location_org_id)`** — a **subset** of sites (several rows) or a **single** site (one row, or a legacy single-site user anchored at their own org).

`accessible_location_ids(user)` is the single source of truth — every scope chokepoint (`scope_org_ids`, `_child_location_ids`, `switch_location`, `current_org_id`, the `/parent` rollup, the location switcher) routes through it. `is_parent_admin()` now means **"a roaming admin"** (manages >1 site) — not "anchored at a parent org," because a main location is itself a selectable site.

**Promotion is in place:** when a standalone single-location org adds its first branch, that org *becomes* the main location in the same row (`type` flips to `'parent'`) — it keeps its patients/devices/users/BAA/login policy, its admins become `all_locations` network admins, and the new branch attaches under it. No synthetic group org, no reparenting.

## v1 locked decisions (R&D-reviewed — do not relitigate)

1. **Adherence calculation window: fixed at 30 days.** Per-org configuration is a future release.
2. **Survey customization deferred to v2** (after the patient mobile app supports configurable questionnaires). v1 ships ABMRC's curated 30/60/90-day questions.
3. **No Inbox SLA alerts in v1** — no "no response in N hours" metric.
4. **Mobile-app ↔ portal authentication is not in v1 scope.** The patient mobile app is already in production, shares this database, and patient user records exist. No new auth scheme needed.
5. **Failed-login policy: no permanent lockout.** After 5 failed attempts, rate-limit with a 30-minute retry delay. Tracked per email + source IP. Counter resets on success or after cooldown.
6. **Therapy summary report: no DMEPOS disclaimer required.**
7. **BAA: signed externally and uploaded as PDF.** No e-signature flow in v1.
8. **Notifications: SMS + email per `users.notify_channel`** — already working. WebSocket browser push is **not** the v1 mechanism.

Single open question for v1.1: BAA storage encryption — AES-GCM with per-tenant key vs. HSM-backed key, pending Security/Compliance sign-off.

Out-of-scope for v1 (not future-promised): per-tenant theming beyond logo, localization beyond en-US, bulk patient CSV import, device telemetry ingestion (separate gateway workstream).

## Code layout (repository root)

```
├── app.py              Flask app, all routes
├── seed.py             Builds arcconnect.db with demo data (Adapt parent + 3 locations, Sunwest as isolation test)
├── schema.sql          SQLite schema
├── requirements.txt    Flask + Pillow
├── arcconnect.db       Pre-seeded demo db (ships in this repo)
├── templates/          Jinja2 templates
├── static/             CSS, JS, uploads/
├── README.md           Setup, demo users, troubleshooting
├── context.md          Design decisions, schema details, route map, build history
├── DEVELOPER_NOTES.md  Implementation notes
└── Dockerfile, docker-compose.yml   Container build
```

Read `context.md` for the design history and `README.md` for runtime details before making non-trivial changes.

## How to run

**Docker (recommended):**
```bash
docker compose up -d
# open http://localhost:5005   (basic-auth gate: Arc / Connect)
```

**Python directly:**
```bash
pip install -r requirements.txt
python seed.py    # only if arcconnect.db is missing or stale
python app.py     # serves on http://localhost:5001
```

Demo logins are listed in `README.md`. Default Docker basic-auth gate is `Arc` / `Connect` (override via `AUTH_USERNAME` / `AUTH_PASSWORD` env vars in compose).

## Working in this codebase

- Flask 3 + SQLite. No ORM — raw `sqlite3` cursor calls in `app.py`. Keep that style; don't introduce SQLAlchemy.
- Templates use server-rendered Jinja2; minimal JS. Don't reach for a frontend framework.
- UI design language: every page wraps in `.page pd-soft`; the page header is a raised band (`.idband` identity bands on dashboard/patient/devices, or a framed `.page-header` on list pages) that floats above flatter framed content cards. Primary buttons are the soft light-blue `.btn-primary`.
- File uploads land in `static/uploads/` for the prototype. Production would move to S3 with signed URLs.
- Auth in the demo is a user-picker (no passwords). Real auth is a v1 work item, not yet implemented.
- When in doubt about scope, prefer the v1 locked decisions above and ask before expanding scope.
