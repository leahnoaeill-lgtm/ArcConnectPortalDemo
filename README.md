# Arc Connect Provider Portal — localhost prototype

A working Flask + SQLite prototype of the HME-facing provider portal. Demonstrates the end-to-end Arc Connect workflow: multi-tenant organizations (main organizations + satellite locations), per-location user management, device inventory, patient-to-device assignment with consent upload, configurable alert rules, and alerts inbox — plus an **ABMRC super-admin console** for managing customer organizations and a **public self-service signup** with Terms acceptance and email OTP verification.

**See `context.md` for design decisions, schema, routes, and build progress.**

---

## Setup

```bash
cd portal
pip install -r requirements.txt
python seed.py                # creates arcconnect.db with demo data
python app.py                 # serves on http://localhost:5001
```

Open <http://localhost:5001> in your browser and pick a demo user.

## Reset

```bash
rm arcconnect.db
rm -rf static/uploads/consent/*.pdf
python seed.py
```

## Docker (persistent data)

```bash
docker compose up -d --build      # serves on http://localhost:5005 (basic-auth gate Arc / Connect)
```

The database (`/app/data`) and uploaded files (`/app/static/uploads`) live on **named Docker volumes**, so they **persist across image rebuilds** — a `docker compose up -d --build` no longer wipes data. Schema changes are applied automatically at startup by idempotent column migrations. To intentionally wipe and reseed: `docker compose down -v && docker compose up -d --build`. The DB path is configurable via the `DB_PATH` env var.

## Demo users

| Email | Role | Org | Scope |
|---|---|---|---|
| `support@abmrespiratory.com` | Super admin | ABM Respiratory Care (ABMRC) | Manufacturer console — customer-org roster, read-only org overview, pending-signup review, USA map |
| `karen.h@adapt.com` | Admin | Adapt (parent HQ) | Sees rollup across all Adapt locations + can switch into any |
| `priya.s@adapt.com` | Admin | Adapt — Denver | Full location admin |
| `james.r@adapt.com` | User | Adapt — Denver | Read-mostly; no settings (per LD-8 the Release 1 role enum is admin / user / super_admin only — "clinician", "billing" etc. are descriptive job titles, not enum values) |
| `linda.w@adapt.com` | Admin | Adapt — Boulder | Different location under same parent |
| `maria.t@sunwest.com` | Admin | Sunwest Medical | Unrelated org (multi-tenant isolation test) |

## Port

Flask runs on **port 5001** (macOS AirPlay often occupies 5000). Override with `PORT=5002 python app.py` if needed.

## File uploads

- Logos → `static/uploads/logos/<org_id>.{png,jpg}`
- Consent forms → `static/uploads/consent/<assignment_id>_<filename>.pdf`

Served directly by Flask in this prototype. In production this would move to S3 with signed URLs + access control.

## What works

**Provider portal (HME / home-health staff)**
- Login / logout (user-picker demo auth)
- Group-admin **network rollup** + switch into any satellite location
- Location dashboard with KPIs; patient list + patient detail
- Device list (All / Assigned / Unassigned), add device, assign to patient with consent PDF
- Alerts inbox (acknowledge / resolve), tasks, inbox
- Settings: org/location info, user management, alert rules, referring clinics/providers
- Multi-tenant isolation (one org can't see another's data)

**ABMRC super-admin console**
- Customer-organization roster (live search + status filter)
- Read-only org overview **hub**: rolled-up patient/device/alert counts, a **satellite-locations** breakdown, compliance (BAA + verification), and the per-org login policy
- Edit organization records (details, verification, BAA upload)
- **Open customer portal** — read-only support view into a customer's live portal (requires a BAA on file)
- Pending-requests **review queue** (approve / reject / request info) + USA overview map

**Self-service organization signup** (`/signup`, public)
- Multi-field intake with **Country-first** address, State / Country dropdowns, and a country-coded phone (default `+1`)
- **Terms-of-Use** acceptance + **email one-time-code (OTP)** verification before a request is recorded
- Approved requests create a customer organization in pending setup

**Per-organization User Login policy**
- Each org chooses **Single Sign-On Allowed** (Google / Facebook / Apple) or **organization email-domain only**
- Default: SSO allowed with all three providers

## What doesn't (yet)

- **Enforced authentication** — sign-in is still the demo user-picker; the per-org login policy (SSO / domain) and the signup email-OTP are captured and exercised, but real password/SSO sign-in isn't wired
- Real device sync (`last_communication` is static)
- Outbound email / SMS and real OTP delivery — notifications and signup codes are logged (and shown on-screen in demo mode), not actually sent
- Patient detail adherence heatmap (shown as placeholder)

BAAs are tracked as simply **on file (with a signed date) or not** — no expiry or revocation. See `context.md` "Known limitations" for the full list.

## Troubleshooting

**"Port 5001 already in use"** — change port: `PORT=5002 python app.py`.

**"Roboto font looks wrong"** — Roboto may not be installed system-wide. The CSS falls back to Helvetica/Arial. Install Roboto for pixel-perfect brand match: <https://fonts.google.com/specimen/Roboto>.

**"Upload doesn't work"** — check `static/uploads/` directories exist and are writable. `seed.py` creates them on first run.
