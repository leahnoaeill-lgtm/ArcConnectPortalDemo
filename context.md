# Arc Connect Portal — Context

**Last updated:** 2026-04-22

## Purpose of this file
Running memory for the Arc Connect Portal build. Read this first on any new session. Update before context runs out.

---

## Project intent

Build a working localhost prototype of the **Arc Connect Provider Portal** that HMEs (Home Medical Equipment companies) use to manage their patients, devices, and alerts. Prototype informs the R&D team's Release 1 build and gets demoed to stakeholders on Day 6 of the kickoff week.

### Core concepts from the product brief
- **Parent / child organization hierarchy.** Large HMEs like Adapt have multiple locations. Each location has its own patients, devices, users, and alert rules. Parent-org admins can see across locations; location admins see only their location.
- **Per-location admins** manage users, location info (logo, address, phone), and alert rules.
- **Device inventory.** Each location adds devices (BiWaze Cough / BiWaze Clear). Tracks: serial number, upload date (added to inventory), assignment date, last communication (default "never"), firmware version. Sees assigned vs unassigned lists.
- **Patient-to-device assignment** requires an uploaded consent form (PDF).
- **Patient list** per location, with status, adherence, device, alerts.
- **Alert configuration** — location admins define what to track, thresholds, notification channels.

---

## Tech stack (decided)

| Layer | Choice | Why |
|---|---|---|
| Backend | Flask (Python 3.9+) | Matches existing `survey_app/` pattern in repo, minimal setup |
| Database | SQLite | No install required, file-based, portable |
| Templates | Jinja2 | Flask default |
| Frontend | Server-rendered HTML + vanilla JS | No Node required |
| Styling | Hand-rolled CSS with Arc Connect brand | Already have the palette |
| Fonts | Roboto / Roboto Light with system fallback | Matches product decks |
| File uploads | Local filesystem under `static/uploads/` | Simple, non-prod |
| Auth | Session-based user picker (demo) | Real password auth is a future layer |

---

## Arc Connect brand palette (extracted from product training deck)

| Token | Hex | Use |
|---|---|---|
| `--ac-cyan` | `#43C1ED` | Primary brand — buttons, active tabs, accents |
| `--ac-cyan-dark` | `#2E8BAD` | Hover / pressed states |
| `--ac-slate` | `#5A656F` | Dark UI chrome, headings |
| `--ac-slate-deep` | `#3D464D` | Top nav background |
| `--ac-gray` | `#A8AEB3` | Borders, tertiary text |
| `--ac-green` | `#00B050` | Adherence OK / on track |
| `--ac-yellow` | `#FCBE03` | Adherence warning |
| `--ac-red` | `#C00000` | Adherence critical / alerts |
| `--ac-bg` | `#F5F7F9` | Page background |
| `--ac-light-cyan` | `#E3F5FC` | Cyan tint for highlights |

Font stack: `'Roboto', 'Helvetica Neue', Helvetica, Arial, sans-serif`

---

## Default decisions (stated to user, proceeding unless told otherwise)

1. **Demo auth** — user picker on login screen; no password. Real auth layered later.
2. **Parent-org admin** — sees rollup view across child locations; can switch into any location via dropdown in top nav. Location admin is scoped to their location only.
3. **Consent form** — required PDF upload at device-assignment time; stored in `static/uploads/consent/`; viewable from Patient → Devices tab and Device → Assignment history. No in-portal e-signature.
4. **Data seeding** — seeded with narrative continuity from the PPT clickable demo (Adapt Respiratory + 3 locations; patients Garcia/Smith/Chen/Wilson/Patel/Kumar/etc.).
5. **Port** — Flask runs on `localhost:5001` (5000 is often taken by AirPlay on Mac).
6. **Alert channels** — email + in-app shown in UI; no actual email send in this prototype (would use SMTP or SendGrid in prod).

---

## Schema summary (see portal/schema.sql for DDL)

```
organizations      ─ parent/child hierarchy; type = 'parent' or 'location'
users              ─ staff at an org; role = admin | clinician | billing | read_only
referring_clinics  ─ external physician practices (per-location directory)
referring_providers─ MDs/NPs/PAs inside a clinic (per-location)
patient_insurance  ─ 1:N with patient; payer + plan + approval date + auth #
patient_prescriptions ─ 1 active row per patient per modality; history retained
patients           ─ belongs to ONE location; optional FKs to clinic + provider; diagnosis
devices            ─ BiWaze Cough or Clear; belongs to ONE location org
device_assignments ─ patient ↔ device with consent_form_path (required)
alert_rules        ─ per-org configurable rules
alerts             ─ triggered events
```

Key FKs always scope by `organization_id` for multi-tenancy.

---

## Routes (Flask app.py)

```
GET  /                          → redirect to login or dashboard
GET  /login                     → user picker
POST /login                     → set session user
GET  /logout                    → clear session

GET  /switch-location           → (parent admins only) pick a location to enter
GET  /parent                    → rollup view with map + clickable KPIs (parent admins)
GET  /parent/patients           → aggregated patient list across all child locations
GET  /parent/devices            → aggregated device list across all child locations
GET  /parent/alerts             → aggregated alerts list across all child locations
GET  /switch-location/<id>?next=<path> → switch into location + deep-link

GET  /                          → location dashboard (when logged in at location)
GET  /patients                  → patient list
GET  /patients/<id>             → patient detail
GET  /patients/new              → add patient form
POST /patients/new              → create patient

GET  /devices                   → all devices (tabs: all / assigned / unassigned)
GET  /devices/new               → add device form
POST /devices/new               → create device
GET  /devices/<id>              → device detail
GET  /devices/<id>/assign       → assign to patient form (requires consent upload)
POST /devices/<id>/assign       → process assignment with consent file
POST /devices/<id>/unassign     → return device

GET  /alerts                    → alerts inbox
POST /alerts/<id>/acknowledge   → ack alert
POST /alerts/<id>/resolve       → resolve alert

GET  /settings                  → location settings (logo/address/phone)
POST /settings                  → update location info
POST /settings/logo             → upload logo

GET  /settings/users            → user list
GET  /settings/users/new        → add user form
POST /settings/users/new        → create user
POST /settings/users/<id>/deactivate → deactivate user

GET  /settings/alert-rules      → alert rules list
GET  /settings/alert-rules/new  → create rule form
POST /settings/alert-rules/new  → create rule
GET  /settings/alert-rules/<id> → edit rule form
POST /settings/alert-rules/<id> → update rule
POST /settings/alert-rules/<id>/delete → delete rule

GET  /uploads/<path>            → serve logo/consent files (auth-gated)
```

---

## File layout

```
portal/
├── app.py                    ← all routes
├── schema.sql                ← DDL (run by init_db on first launch)
├── seed.py                   ← populates demo data
├── requirements.txt          ← flask, pillow (for logo thumbnail)
├── README.md                 ← run instructions
├── context.md                ← THIS FILE
├── arcconnect.db             ← SQLite (gitignored)
├── static/
│   ├── css/arc.css           ← brand styling
│   ├── js/arc.js             ← light interactivity
│   └── uploads/
│       ├── logos/            ← per-location PNG/JPG
│       └── consent/          ← per-assignment PDF
└── templates/
    ├── base.html             ← shared layout (top nav, flash msgs)
    ├── login.html            ← user picker
    ├── parent_overview.html  ← rollup for parent-org admins
    ├── dashboard.html        ← location dashboard
    ├── patients.html
    ├── patient_detail.html
    ├── patient_form.html
    ├── devices.html
    ├── device_form.html
    ├── device_detail.html
    ├── device_assign.html    ← with consent upload
    ├── alerts.html
    ├── settings.html         ← location info + logo
    ├── users.html
    ├── user_form.html
    ├── alert_rules.html
    └── alert_rule_form.html
```

---

## Build progress

- [x] Directory structure
- [x] context.md
- [x] README.md + requirements.txt
- [x] schema.sql
- [x] seed.py (demo data)
- [x] app.py (all routes)
- [x] CSS
- [x] Base template + all page templates
- [x] **Smoke test PASSED** (2026-04-22)

### Smoke test results
All 16 routes tested for Priya (Denver admin): 200 OK.
Parent-admin routes tested for Karen: 200 OK including `/parent` rollup and `/switch-location/<id>`.
Multi-tenant isolation verified: Maria (Sunwest) gets 404 on Adapt's patients/devices.
No errors or tracebacks in the app log.

---

## Open design questions

*(None currently blocking — all defaults stated above. Awaiting user confirmation.)*

### Questions I may raise as build progresses
- Should deactivated users be visible in the list with a "deactivated" badge, or fully hidden?
- Logo — should we enforce a max size/aspect ratio?
- Alert rules — do we allow per-patient overrides, or only org-wide rules? (MVP: org-wide only.)
- Patient status field — just 'active' or also 'inactive', 'paused', 'deceased'? (MVP: active/inactive.)

---

## Known limitations / future work

- **No real auth.** User picker only; no passwords, no MFA, no session timeout.
- **No email sending.** Alert notifications are stored in DB but not sent; would plug in SMTP / SendGrid in prod.
- **No device data sync.** `last_communication` is static from seed; no real BLE/backend sync in prototype.
- **Session data is synthetic.** `therapy_sessions` table is seeded with 1,490 procedurally-generated rows (45 days × up to 2/day × 17 patients × modalities). Waveforms are shape-accurate but not real device payloads. Therapy goal hardcoded: 2 sessions/day/modality. Per-modality adherence computed from actual session counts capped at goal.
- **No audit log.** HIPAA access log table exists in the R&D ERD but is out of scope for the prototype.
- **Single Flask process.** Not production-ready (no gunicorn, no HTTPS, no CSRF protection). Fine for localhost.
- **File uploads** served directly by Flask from static/ — in prod would use S3 + signed URLs.

---

## How to run

```bash
cd portal
pip install -r requirements.txt
python seed.py          # creates arcconnect.db with demo data
python app.py           # serves on http://localhost:5001
```

See README.md for detailed instructions.

---

## Demo narrative / users

Login as one of these to explore:

| User | Role | Org | What they see |
|---|---|---|---|
| `priya.s@adapt.com` | Admin | Adapt — Denver | Full location admin view |
| `james.r@adapt.com` | Clinician | Adapt — Denver | Same screens, no settings access |
| `karen.h@adapt.com` | Admin | Adapt (parent HQ) | Rollup + can switch into any location |
| `maria.t@sunwest.com` | Admin | Sunwest Medical | A second org for multi-tenant testing |
