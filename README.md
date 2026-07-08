# Arc Connect Provider Portal — localhost prototype

A working Flask + SQLite prototype of the HME-facing provider portal. Demonstrates the end-to-end Arc Connect workflow: multi-tenant organizations (main organizations + satellite locations), per-location user management, device inventory, patient-to-device assignment with consent upload, configurable alert rules, and alerts inbox — plus an **ABMRC super-admin console** for managing customer organizations and a **public self-service signup** (Terms acceptance + email OTP) that feeds an **onboarding lifecycle**: Submitted → New → Pending → Ready → Active.

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

**Demo kiosk mode (reset on logout).** With `DEMO_RESET_ON_LOGOUT=1` (on by default in `docker-compose.yml`), every logout restores the database and uploads from the pristine golden snapshot baked into the image at `/app/golden` (fresh `seed.py` + `backfill_heatmap_demo.py` output plus the seeded upload files). Demo viewers therefore can't leave the portal in a messed-up state — anything they change, add, or upload is discarded when they log out. Note the reset is global (one viewer's logout resets the data for everyone currently browsing). Unset the env var to turn it off; running `python app.py` locally is unaffected.

## Demo users

| Email | Role | Org | Scope |
|---|---|---|---|
| `support@abmrespiratory.com` | Super admin | ABM Respiratory Care (ABMRC) | Manufacturer console — customer-org roster, read-only org overview, **Submitted** queue, USA map |
| `karen.h@adapt.com` | Admin | Adapt (parent HQ) | Sees rollup across all Adapt locations + can switch into any |
| `priya.s@adapt.com` | Admin | Adapt — Denver | Full location admin |
| `james.r@adapt.com` | User | Adapt — Denver | Read-mostly; no settings (per LD-8 the Release 1 role enum is admin / user / super_admin only — "clinician", "billing" etc. are descriptive job titles, not enum values) |
| `linda.w@adapt.com` | Admin | Adapt — Boulder | Different location under same parent |
| `maria.t@sunwest.com` | Admin | Sunwest Medical | Unrelated org (multi-tenant isolation test) |
| `admin@bigskyhh.com` | Admin | Big Sky Home Health (**New**) | Group admin of a pre-Active account — shows the "pending activation" banner + limited view |

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
- **Customer-organization roster** (live search + status filter) — only verified orgs appear here
- **Submitted** queue: organizations that self-registered on the website. **Verify** a submission (super-admin only) to add it to the org table as **New** — keeps the roster clean until a request is vetted
- Read-only org overview **hub**: rolled-up patient/device/alert counts, a **satellite-locations** breakdown, compliance (BAA + verification), and the per-org login policy
- **Edit** organization records — all fields optional, so a partial record can be saved
- **Activate** a Ready org; **Open customer portal** (read-only support view, requires a BAA on file); suspend / reactivate; USA overview map

**Self-service organization signup** (`/signup`, public)
- Multi-field intake with **Country-first** address, State / Country dropdowns, and a country-coded phone (default `+1`)
- **Terms-of-Use** acceptance + **email one-time-code (OTP)** verification before the request is recorded
- Submissions land in the super admin's **Submitted** queue; verifying one adds the org to the table as **New**

**Per-organization User Login policy**
- Each org chooses **Single Sign-On Allowed** (Google / Facebook / Apple) or **organization email-domain only**
- Default: SSO allowed with all three providers

## Organization lifecycle

A customer organization moves through these statuses:

| Status | Meaning | How it advances |
|---|---|---|
| **Submitted** | A self-registration from the website — lives in the super admin's *Submitted* queue, **not** in the org table yet | super admin **Verify** → New |
| **New** | Verified; now in the org table, no BAA/verification yet | save a BAA *or* verification → Pending |
| **Pending** | A BAA **or** account verification has been saved | save the other one → Ready |
| **Ready** | **Both** a BAA and verification are on file | super admin **Activate** → Active |
| **Active** | Live | (super admin can Suspend) |
| **Suspended** | Manually paused | Reactivate → Active |

A **group admin can sign in while their account is pre-Active** (New / Pending / Ready). They see a **"pending activation" banner** and a limited view — they **cannot add satellite locations, users, or devices** until a super admin activates the organization.

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
