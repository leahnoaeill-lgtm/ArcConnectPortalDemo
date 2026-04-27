# Arc Connect Provider Portal — Developer Notes

Housekeeping, production checklist, and design decisions worth flagging
for engineers picking this project up. Keep short and current.

---

## Production deployment checklist

Things that are acceptable in this demo build but **must** be addressed
before any real patient data touches this application.

### 1. Turn off Flask debug mode
`app.py` ends with:
```python
app.run(host='127.0.0.1', port=port, debug=True)
```
Debug mode enables the Werkzeug interactive debugger, which is a
remote-code-execution risk if exposed. Before deploying, drive this from
an env var:
```python
app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG') == '1')
```
Never set `FLASK_DEBUG=1` in production.

### 2. Add CSRF protection to all POST forms
Not present in the demo. Recommended path:
```bash
pip install Flask-WTF
```
Then in `app.py`:
```python
from flask_wtf.csrf import CSRFProtect
csrf = CSRFProtect(app)
```
Every `<form method="POST">` must then include `{{ csrf_token() }}` inside
a hidden input. Templates to update (every template with a POST form —
see `grep -rn 'method="POST"' templates/`): tasks, alerts, inbox, patient
forms, settings, referring-*, etc. JSON API endpoints can be exempted per-route
with `@csrf.exempt` when they use a different auth scheme.

### 3. Password authentication
Current demo uses a `SELECT user` by id with no password check — fine for a
local demo, but blocker for real use. Either:
- Drop a proper password hash column on `users` and use `werkzeug.security.generate_password_hash` / `check_password_hash`, OR
- Replace with an SSO integration (Okta, Azure AD) — most HME / home-health orgs already have one.

### 4. Externalize the secret key
`app.secret_key` is hardcoded in `app.py`. Read from an env var (or a
cloud secret manager) at startup and fail loud if unset.

### 5. HTTPS enforcement
Behind a load balancer, set `ProxyFix` and ensure cookies are marked
`Secure` + `HttpOnly` + `SameSite=Lax` (or stricter).
```python
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Lax')
```

### 6. Database migration path
Schema changes right now are destructive — `init_db()` drops the sqlite
file. Before production, migrate to Alembic (or equivalent) so schema
updates are non-destructive.

### 7. Storage for uploads
Consent forms and location logos are written to the local filesystem
(`static/uploads/`). Move to S3 / Azure Blob / GCS for production so
horizontal scaling works.

### 8. Notifications are stubs
`_task_notify()` in `app.py` logs email/SMS events via `app.logger`. No
real delivery happens. Wire up SendGrid (or SES) and Twilio (or similar)
before shipping.

### 9. Mobile app authentication
`/api/patient/<id>/features` is currently only gated by the portal session
cookie, which means it's effectively unauthenticated for the mobile app.
Define a token scheme (per-patient bearer token, for example) before
exposing beyond localhost.

### 10. Audit log coverage
`access_log` is written by the portal, but **any script or backfill that
bypasses the Flask routes** will not show up. Keep this in mind when
writing admin scripts — prefer exposing admin actions as routes so the
audit log stays complete.

---

## Design decisions / invariants

Keep these in mind when extending the codebase.

### Inbox is a view, not a state machine
Every `inbox_item` has a linked `task` (created by seed + auto-created when
a message / mood / survey arrives). The task owns status, due-date, and
assignee. The inbox page reads those fields via `LEFT JOIN tasks` — do not
write to `inbox_items.status` or `inbox_items.assigned_to_user_id` directly;
update the task and let the inbox reflect it.

### Task statuses are the single vocabulary
`todo`, `in_progress`, `pending_external`, `completed`, `cancelled`.
Applies to: tasks, inbox items, message-response workflow, mood-note
follow-up, and survey review. Any screen that shows "state" for engagement
work should use these labels.

### Feature-flag hierarchy
`feature_enabled(flag, org_id)` in `app.py`:
- Returns `True` only when both the location and (if present) its parent
  have the flag enabled.
- Parent "messaging disabled" overrides all child locations.
- The parent override cascades for **messaging AND mood free-text response**
  (treated as one engagement bundle).

### Role vocabulary
- `admin` — full control within their organization scope
- `clinician` — reads/writes patients, tasks, alerts, inbox, referrals
- `billing` — reads patients, prescriptions, insurance; no therapy data writes
- `read_only` — no writes anywhere

Destructive actions (hard-delete) are `@require_admin`. Add/edit/deactivate
on referring clinics and providers are open to any logged-in user per
product decision.

### Organization scoping
Every list query filters by `organization_id = current_org_id()`. Parent
admins hit specialized `/parent/*` views that union across child locations.
When adding a new list-type view, default to location-scoped and add a
parent variant separately; don't weaken the location filter.

### Date / time formatting
Three filters in `app.py`: `dt` (DD-MMM-YYYY h:mm AM/PM), `date_only` (DD-MMM-YYYY),
`time_only` (h:mm AM/PM). HTML `<input type="date">` bindings still use ISO
`YYYY-MM-DD` because the browser requires it — don't attempt to reformat
those.

### Client-side sort / filter
Universal helper in `base.html` — any `<table class="sortable">` with
`data-sort-key` headers and matching `data-sort-<key>` row attributes gets
click-to-sort for free. New list pages should use this pattern rather than
inventing server-side sort for every column.

### Brand palette discipline
Defined in `static/css/arc.css`. Rules:
- BiWaze Cough → `--biwaze-cough` (orange, #F27C1C)
- BiWaze Clear → `--biwaze-clear` (aliased to `--ac-cyan`, #43C1ED)
- All other UI uses Arc Connect palette only (cyan / slate / green / yellow / red)
- Don't introduce ad-hoc colors. If you need a new accent, add it to `:root` with a name.

### Serial number masks
- BiWaze Cough: `CNSXXAXXX`
- BiWaze Clear: `KNSXXAXXX`
- X = decimal digit.
- `_gen_serial()` in `seed.py` produces mask-conformant values for demo data.
- Form validation regex: `[CK]NS\d{2}A\d{3}` (applied in `device_form.html`).

### Access log writes
Every patient-data access event should call `_log_access(event, patient_id, ref_type, ref_id, detail)`
(helper to be added in Batch 4). Events to track: `patient_view`, `session_view`,
`report_generate`, `data_export`, `task_activity`. Keep this in sight when
adding new patient-data surfaces.

---

## Known trade-offs and deferred items

| Item | Status | Notes |
|---|---|---|
| Bulk action UI on alerts/tasks | Planned | Select-rows + bulk toolbar. |
| Calendar view for tasks | Planned | Weekly grid + saved preference. |
| Per-session printable report | Planned | Extend existing report. |
| Read-receipt to mobile app | Planned | Ping on provider reply. |
| Patient-referral history UI | Planned | Table exists (`patient_referral_history`), display TBD. |
| Concurrent-write race (task/inbox) | Mitigated | Inbox now derives from task — single source of truth. |
| Survey opt-out UI | Planned | Columns exist (`opted_out`, `opted_out_at`). |
| Two-step "Resolve" confirmation on alerts | Planned | Prevents mis-clicks vs Acknowledge. |
| Responsive breakpoint | Planned | Push 900px → 1024px and add `overflow-x: auto` on card bodies. |

---

## Testing

There are currently no automated tests. For a demo this is acceptable;
before production, at minimum add:
- Unit tests on `feature_enabled()` flag hierarchy behavior.
- Integration tests on the bidirectional task ↔ inbox sync paths.
- A login-fixture to seed a known user and exercise each main route.

A Flask test client + `pytest` would be sufficient.
