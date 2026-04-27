# Arc Connect Provider Portal — localhost prototype

A working Flask + SQLite prototype of the HME-facing provider portal. Demonstrates the end-to-end Arc Connect workflow: multi-tenant organizations with parent/child locations, per-location user management, device inventory, patient-to-device assignment with consent upload, configurable alert rules, and alerts inbox.

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

## Demo users

| Email | Role | Org | Scope |
|---|---|---|---|
| `karen.h@adapt.com` | Admin | Adapt (parent HQ) | Sees rollup across all Adapt locations + can switch into any |
| `priya.s@adapt.com` | Admin | Adapt — Denver | Full location admin |
| `james.r@adapt.com` | Clinician | Adapt — Denver | Read-mostly; no settings |
| `linda.w@adapt.com` | Admin | Adapt — Boulder | Different location under same parent |
| `maria.t@sunwest.com` | Admin | Sunwest Medical | Unrelated org (multi-tenant isolation test) |

## Port

Flask runs on **port 5001** (macOS AirPlay often occupies 5000). Override with `PORT=5002 python app.py` if needed.

## File uploads

- Logos → `static/uploads/logos/<org_id>.{png,jpg}`
- Consent forms → `static/uploads/consent/<assignment_id>_<filename>.pdf`

Served directly by Flask in this prototype. In production this would move to S3 with signed URLs + access control.

## What works

- Login / logout (user picker auth)
- Parent-org rollup view + switch-location
- Location dashboard with KPIs
- Patient list + patient detail
- Device list (All / Assigned / Unassigned tabs)
- Add device
- Assign device to patient with consent PDF upload
- Alerts inbox with acknowledge / resolve
- Location settings (update name, address, phone, upload logo)
- User management (add / deactivate)
- Alert rule CRUD (metric, threshold, severity, channels)
- Multi-tenant isolation (one org can't see another's data)

## What doesn't (yet)

- Real auth (no passwords)
- Real device sync (last_communication is static)
- Outbound email on alerts
- Editing an existing device or patient after creation
- Patient detail adherence heatmap (shown as placeholder)

See `context.md` "Known limitations" for the full list.

## Troubleshooting

**"Port 5001 already in use"** — change port: `PORT=5002 python app.py`.

**"Roboto font looks wrong"** — Roboto may not be installed system-wide. The CSS falls back to Helvetica/Arial. Install Roboto for pixel-perfect brand match: <https://fonts.google.com/specimen/Roboto>.

**"Upload doesn't work"** — check `static/uploads/` directories exist and are writable. `seed.py` creates them on first run.
