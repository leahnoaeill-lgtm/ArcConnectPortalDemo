#!/usr/bin/env python3
"""Arc Connect Provider Portal — Flask prototype.
Run:  python seed.py && python app.py
Open: http://localhost:5001
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, date
from pathlib import Path
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, send_from_directory, abort, jsonify, Response)
from werkzeug.utils import secure_filename

HERE = Path(__file__).parent
DB_PATH = HERE / 'arcconnect.db'
UPLOADS_DIR = HERE / 'static' / 'uploads'
LOGOS_DIR = UPLOADS_DIR / 'logos'
CONSENT_DIR = UPLOADS_DIR / 'consent'
RX_DIR = UPLOADS_DIR / 'prescriptions'
ALLOWED_LOGO_EXT = {'png', 'jpg', 'jpeg', 'gif', 'svg'}
ALLOWED_CONSENT_EXT = {'pdf', 'png', 'jpg', 'jpeg'}
ALLOWED_RX_EXT = {'pdf', 'doc', 'docx', 'jpg', 'jpeg', 'png', 'tif', 'tiff'}
MAX_UPLOAD_MB = 10

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'arc-connect-portal-demo-key-2026')
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024


def _ensure_heatmap_schema():
    """Lightweight migration so an existing arcconnect.db gets the heatmap
    tables without requiring a full re-seed. Idempotent."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS heatmap_settings (
            organization_id INTEGER PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
            layers_json TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
        )""")
        conn.commit()
    finally:
        conn.close()


_ensure_heatmap_schema()


# ── Optional HTTP Basic Auth gate (set AUTH_USERNAME + AUTH_PASSWORD env vars to enable) ──

_BASIC_AUTH_USER = os.environ.get('AUTH_USERNAME')
_BASIC_AUTH_PASS = os.environ.get('AUTH_PASSWORD')


@app.before_request
def _require_basic_auth():
    if not (_BASIC_AUTH_USER and _BASIC_AUTH_PASS):
        return None
    auth = request.authorization
    if auth and auth.username == _BASIC_AUTH_USER and auth.password == _BASIC_AUTH_PASS:
        return None
    return Response(
        'Authentication required.', 401,
        {'WWW-Authenticate': 'Basic realm="Arc Connect Portal"'}
    )


# ── DB helpers ───────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(_exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def q_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def q_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def q_exec(sql, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


# ── Auth (demo: session-based user picker) ──────────────────────────────────

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return q_one('SELECT u.*, o.name AS org_name, o.type AS org_type, o.parent_id AS org_parent '
                 'FROM users u JOIN organizations o ON o.id = u.organization_id '
                 'WHERE u.id = ?', (uid,))


def current_org_id():
    """The org the user is CURRENTLY acting within. For parent-org admins who've
    switched into a location, this is the location id. For location users, this
    is their user's organization_id. For super admins drilled into a customer
    org, this returns that customer org's id (read-only context)."""
    u = current_user()
    if not u:
        return None
    if is_super_admin() and session.get('super_acting_org_id'):
        return session['super_acting_org_id']
    return session.get('acting_org_id') or u['organization_id']


def current_org():
    oid = current_org_id()
    if not oid:
        return None
    return q_one('SELECT * FROM organizations WHERE id = ?', (oid,))


def is_parent_admin():
    u = current_user()
    if not u:
        return False
    return u['role'] == 'admin' and u['org_type'] == 'parent'


def is_super_admin():
    """ABMRC's internal super admin role. Cross-org read-only access governed
    by an active BAA per customer organization."""
    u = current_user()
    return bool(u and u['role'] == 'super_admin')


def super_acting_org_id():
    """When a super admin has drilled into a customer org for read-only
    inspection, this returns that org's id (set in session). None when not
    drilled in."""
    if not is_super_admin():
        return None
    return session.get('super_acting_org_id')


def scope_org_ids():
    """Return the list of org ids the current user's list-style queries
    should span. For a parent admin at rollup, that's every child location.
    For a super admin drilled into a customer org, it's the customer parent +
    its child locations. For everyone else, just their current acting org."""
    u = current_user()
    if not u:
        return []
    # Super admin drilled into a customer org → their parent + children
    if is_super_admin() and session.get('super_acting_org_id'):
        oid = session['super_acting_org_id']
        rows = q_all(
            "SELECT id FROM organizations WHERE id = ? OR parent_id = ?",
            (oid, oid))
        return [r['id'] for r in rows]
    if is_parent_admin() and not session.get('acting_org_id'):
        rows = q_all("SELECT id FROM organizations WHERE parent_id = ? AND type = 'location'",
                     (u['organization_id'],))
        return [r['id'] for r in rows]
    return [current_org_id()]


def can_read_patient(patient_id):
    """Returns the patient row if the current user is allowed to read it,
    None otherwise. Super admins drilled into a customer org can read any
    patient under that org's scope."""
    p = q_one('SELECT * FROM patients WHERE id = ?', (patient_id,))
    if not p:
        return None
    if p['organization_id'] in scope_org_ids():
        return p
    return None


def is_rollup_scope():
    """True when caller is viewing a parent rollup — applies to both parent
    admins at rollup and super admins drilled into a customer parent org."""
    if is_parent_admin() and not session.get('acting_org_id'):
        return True
    if is_super_admin() and session.get('super_acting_org_id'):
        # Only treat as rollup if the customer org has child locations.
        oid = session['super_acting_org_id']
        org = q_one('SELECT type FROM organizations WHERE id = ?', (oid,))
        if org and org['type'] == 'parent':
            return True
    return False


def effective_parent_org_id():
    """The 'account' org id whose rollup is being viewed. Karen → her own org.
    Super admin acting in Adapt → Adapt's id."""
    u = current_user()
    if not u: return None
    if is_super_admin() and session.get('super_acting_org_id'):
        return session['super_acting_org_id']
    if is_parent_admin():
        return u['organization_id']
    return None


def alert_rules_context():
    """Returns (effective_org_id, parent_managed, can_edit) for the current user.

    When the parent has alert_rules_source='parent', every child location reads
    the parent's rule set and location admins cannot edit. Parent admins can
    always edit the parent's set from rollup. Location admins are only allowed
    to edit their own rules when the parent is set to 'location' mode.
    """
    u = current_user()
    if not u:
        return None, False, False
    oid = current_org_id()
    org = q_one('SELECT id, parent_id FROM organizations WHERE id = ?', (oid,))
    if not org:
        return oid, False, False
    if org['parent_id']:
        parent = q_one('SELECT id, alert_rules_source FROM organizations WHERE id = ?',
                       (org['parent_id'],))
        if parent and parent['alert_rules_source'] == 'parent':
            # Child reads parent's rules, cannot edit.
            return parent['id'], True, False
        return oid, False, True
    # Parent org viewing their own rules — always editable.
    return oid, False, True


def feature_enabled(flag_name, org_id=None):
    """Return True only when the given feature is enabled both at the location
    AND its parent (if any). The parent's messaging flag acts as a master
    kill-switch for the whole engagement bundle: when parent messaging is
    disabled, both messaging and mood_response_enabled are forced off for all
    child locations regardless of local setting.

    flag_name: 'messaging_enabled' or 'mood_response_enabled'.
    """
    if flag_name not in ('messaging_enabled', 'mood_response_enabled'):
        raise ValueError(f'Unknown feature flag: {flag_name}')
    oid = org_id if org_id is not None else current_org_id()
    if not oid: return False
    row = q_one("""SELECT messaging_enabled, mood_response_enabled, parent_id
                   FROM organizations WHERE id = ?""", (oid,))
    if not row: return False
    if not row[flag_name]: return False
    if row['parent_id']:
        prow = q_one("""SELECT messaging_enabled, mood_response_enabled
                        FROM organizations WHERE id = ?""", (row['parent_id'],))
        if prow:
            # Parent "messaging off" is the master override — kills engagement entirely.
            if not prow['messaging_enabled']:
                return False
            # Parent's specific flag also overrides the child's same flag.
            if not prow[flag_name]:
                return False
    return True


# Whitelist of POST endpoints a super admin IS allowed to hit. Everything else
# is read-only; submitting a form refuses with 403.
SUPER_ADMIN_WRITE_ALLOWLIST = {
    'login', 'logout', 'super_org_new', 'super_org_suspend', 'super_org_activate',
    'super_baa_new', 'super_user_invite', 'super_set_acting_org', 'super_clear_acting_org',
}


@app.before_request
def _enforce_super_admin_readonly():
    """When a super admin makes a state-changing request, only allow the
    explicitly whitelisted super-admin endpoints. This is the single chokepoint
    that enforces 'view-only across customer data covered by BAA'."""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    if not is_super_admin():
        return None
    if (request.endpoint or '') in SUPER_ADMIN_WRITE_ALLOWLIST:
        return None
    flash('Super admins have view-only access to customer data.', 'error')
    return redirect(request.referrer or url_for('home'))


def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u or u['role'] != 'admin':
            flash('Admin access required.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return wrapper


def _log_access(event, patient_id=None, ref_type=None, ref_id=None, detail=None,
                justification=None):
    """Write an entry to access_log. Non-fatal — we never want a logging
    failure to break a page render, so wrap writes and swallow exceptions.

    When the actor is an ABMRC super admin, the row is flagged as external
    so the customer parent admin can see "ABMRC accessed our data" on a
    dedicated audit tab (reciprocal audit / accounting of disclosures)."""
    try:
        u = current_user()
        oid = current_org_id()
        is_external = 1 if is_super_admin() else 0
        q_exec("""INSERT INTO access_log
                  (organization_id, user_id, event, patient_id, ref_type, ref_id,
                   detail, is_external_access, justification)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (oid, u['id'] if u else None, event, patient_id,
                ref_type, ref_id, detail, is_external, justification))
    except Exception as e:
        app.logger.warning(f'access_log write failed: {e}')


@app.context_processor
def inject_globals():
    u = current_user()
    org = current_org()
    parent_org = None
    sibling_locations = []
    if u and u['org_type'] == 'location' and u['org_parent']:
        parent_org = q_one('SELECT * FROM organizations WHERE id = ?',
                           (u['org_parent'],))
        sibling_locations = q_all(
            'SELECT * FROM organizations WHERE parent_id = ? AND type = "location" ORDER BY name',
            (u['org_parent'],))
    elif is_parent_admin():
        # Parent admin — see all child locations
        parent_org = q_one('SELECT * FROM organizations WHERE id = ?',
                           (u['organization_id'],))
        sibling_locations = q_all(
            'SELECT * FROM organizations WHERE parent_id = ? AND type = "location" ORDER BY name',
            (u['organization_id'],))
    from datetime import datetime as _dt_now
    # Inbox unread count (only meaningful when messaging is on for this location)
    inbox_unread = 0
    messaging_on = False
    mood_resp_on = False
    if u:
        messaging_on = feature_enabled('messaging_enabled')
        mood_resp_on = feature_enabled('mood_response_enabled')
        if messaging_on:
            _row = q_one("""SELECT COUNT(*) AS n FROM inbox_items i
                            LEFT JOIN tasks t ON t.inbox_item_id = i.id
                            WHERE i.organization_id = ?
                              AND COALESCE(t.status, 'todo') = 'todo'""",
                         (current_org_id(),))
            inbox_unread = _row['n'] if _row else 0
    return {
        'current_user': u,
        'current_org': org,
        'parent_org': parent_org,
        'sibling_locations': sibling_locations,
        'is_parent_admin': is_parent_admin(),
        'is_super_admin': is_super_admin(),
        'super_acting_org_id': super_acting_org_id(),
        'now_iso': _dt_now.now().isoformat(sep=' ', timespec='seconds'),
        'messaging_enabled': messaging_on,
        'mood_response_enabled': mood_resp_on,
        'inbox_unread': inbox_unread,
    }


# ── Auth routes ──────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user_id = request.form.get('user_id', type=int)
        u = q_one('SELECT * FROM users WHERE id = ? AND is_active = 1', (user_id,))
        if not u:
            flash('User not found or deactivated.', 'error')
            return redirect(url_for('login'))
        session.clear()
        session['user_id'] = u['id']
        q_exec('UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?',
               (u['id'],))
        return redirect(url_for('home'))

    users = q_all("""
        SELECT u.*, o.name AS org_name, o.type AS org_type,
               po.name AS parent_org_name
        FROM users u
        JOIN organizations o ON o.id = u.organization_id
        LEFT JOIN organizations po ON po.id = o.parent_id
        WHERE u.is_active = 1
        ORDER BY o.name, u.role DESC, u.last_name
    """)
    return render_template('login.html', users=users)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/switch-location/<int:org_id>')
@require_login
def switch_location(org_id):
    # Only parent admins can switch into a child location; or an admin can reset
    # back to their own org.
    u = current_user()
    target = q_one('SELECT * FROM organizations WHERE id = ?', (org_id,))
    if not target:
        abort(404)
    # Optional deep-link: after switching, land on this path instead of /dashboard
    next_url = request.args.get('next', '')
    safe_next = next_url if next_url.startswith('/') and '//' not in next_url else None
    # Allow if parent admin and target is a child of their parent org
    if is_parent_admin() and target['parent_id'] == u['organization_id']:
        session['acting_org_id'] = org_id
        return redirect(safe_next or url_for('dashboard'))
    # Allow if the user's own org (clears any switch). For parent admins that
    # means returning to group-wide rollup, so default to the parent overview.
    if org_id == u['organization_id']:
        session.pop('acting_org_id', None)
        if is_parent_admin():
            return redirect(safe_next or url_for('parent_overview'))
        return redirect(safe_next or url_for('dashboard'))
    flash('You do not have permission to access that location.', 'error')
    return redirect(url_for('home'))


# ── Home routing ─────────────────────────────────────────────────────────────

@app.route('/')
def home():
    u = current_user()
    if not u:
        return redirect(url_for('login'))
    # ABMRC super admin → their dedicated dashboard.
    if is_super_admin():
        return redirect(url_for('super_dashboard'))
    # Parent-org admin with no acting location → go to rollup
    if is_parent_admin() and not session.get('acting_org_id'):
        return redirect(url_for('parent_overview'))
    return redirect(url_for('dashboard'))


# ── Parent-org rollup ────────────────────────────────────────────────────────

def _child_location_ids(user):
    """All child location IDs under this parent admin's org."""
    rows = q_all("SELECT id FROM organizations WHERE parent_id = ? AND type = 'location'",
                 (user['organization_id'],))
    return [r['id'] for r in rows]


def _require_parent_rollup_scope():
    """Returns (ids, None) if the caller is a parent admin at rollup scope OR
    a super admin drilled into a customer parent org. Otherwise bounces them."""
    u = current_user()
    if is_parent_admin():
        return _child_location_ids(u), None
    if is_super_admin() and session.get('super_acting_org_id'):
        oid = session['super_acting_org_id']
        rows = q_all(
            "SELECT id FROM organizations WHERE parent_id = ? AND type = 'location'",
            (oid,))
        return [r['id'] for r in rows], None
    return None, redirect(url_for('dashboard'))


@app.route('/parent')
@require_login
def parent_overview():
    u = current_user()
    parent_id = effective_parent_org_id()
    # Allowed for parent admins and for super admins drilled into a parent org.
    if not parent_id or not (is_parent_admin() or
                             (is_super_admin() and session.get('super_acting_org_id'))):
        return redirect(url_for('dashboard'))
    locations = q_all(
        """SELECT o.*,
           (SELECT COUNT(*) FROM patients p WHERE p.organization_id = o.id AND p.status = 'active') AS patient_count,
           (SELECT COUNT(*) FROM patients p WHERE p.organization_id = o.id AND p.status = 'active'
                AND p.adherence_pct_30d IS NOT NULL AND p.adherence_pct_30d < 80) AS at_risk_count,
           (SELECT COUNT(*) FROM devices d WHERE d.organization_id = o.id) AS device_count,
           (SELECT COUNT(*) FROM devices d WHERE d.organization_id = o.id AND d.status = 'in_use') AS devices_in_use,
           (SELECT COUNT(*) FROM alerts a WHERE a.organization_id = o.id AND a.resolved_at IS NULL) AS active_alerts,
           (SELECT COUNT(*) FROM alerts a WHERE a.organization_id = o.id AND a.resolved_at IS NULL AND a.severity = 'critical') AS critical_alerts,
           (SELECT ROUND(AVG(p.adherence_pct_30d)) FROM patients p
                WHERE p.organization_id = o.id AND p.status = 'active') AS avg_adherence,
           (SELECT COUNT(*) FROM users u2 WHERE u2.organization_id = o.id AND u2.is_active = 1) AS user_count
           FROM organizations o WHERE o.parent_id = ? AND o.type = 'location' ORDER BY o.name""",
        (parent_id,))
    totals = {
        'patients': sum(l['patient_count'] for l in locations),
        'devices': sum(l['device_count'] for l in locations),
        'devices_in_use': sum(l['devices_in_use'] for l in locations),
        'active_alerts': sum(l['active_alerts'] for l in locations),
        'critical_alerts': sum(l['critical_alerts'] for l in locations),
    }

    # ── Adherence distribution histogram (across all active patients) ──
    child_ids = [l['id'] for l in locations]
    buckets = [
        {'label': '0–19%',  'range': (0, 20),   'count': 0, 'class': 'status-red'},
        {'label': '20–39%', 'range': (20, 40),  'count': 0, 'class': 'status-red'},
        {'label': '40–59%', 'range': (40, 60),  'count': 0, 'class': 'status-yellow'},
        {'label': '60–79%', 'range': (60, 80),  'count': 0, 'class': 'status-yellow'},
        {'label': '80–100%','range': (80, 101), 'count': 0, 'class': 'status-green'},
    ]
    unknown_count = 0
    if child_ids:
        ph = ','.join('?' * len(child_ids))
        pts = q_all(f"""SELECT adherence_pct_30d FROM patients
                        WHERE organization_id IN ({ph}) AND status = 'active'""",
                    tuple(child_ids))
        for row in pts:
            a = row['adherence_pct_30d']
            if a is None:
                unknown_count += 1
            else:
                for b in buckets:
                    lo, hi = b['range']
                    if lo <= a < hi:
                        b['count'] += 1
                        break
    bucket_total = sum(b['count'] for b in buckets) or 1
    bucket_max = max((b['count'] for b in buckets), default=0) or 1
    for b in buckets:
        b['pct_of_total'] = round(100 * b['count'] / bucket_total) if bucket_total else 0
        b['bar_width_pct'] = round(100 * b['count'] / bucket_max) if bucket_max else 0

    # ── Leaderboard metrics (per location, with rank coloring) ──
    # Filter to locations with actual patient data
    rankable = [l for l in locations if l['patient_count'] > 0]
    # Build per-location dict of metrics
    loc_metrics = []
    for l in rankable:
        at_risk_pct = round(100 * l['at_risk_count'] / l['patient_count']) if l['patient_count'] else 0
        crit_per_100 = round(100 * l['critical_alerts'] / l['patient_count'], 1) if l['patient_count'] else 0
        open_per_100 = round(100 * l['active_alerts'] / l['patient_count'], 1) if l['patient_count'] else 0
        device_util = round(100 * l['devices_in_use'] / l['device_count']) if l['device_count'] else 0
        loc_metrics.append({
            'id': l['id'], 'name': l['name'], 'city': l['city'], 'state': l['state'],
            'patient_count': l['patient_count'],
            'values': {
                'avg_adherence':     l['avg_adherence'] or 0,
                'at_risk_pct':       at_risk_pct,
                'crit_per_100':      crit_per_100,
                'open_per_100':      open_per_100,
                'device_util':       device_util,
            }
        })

    # Metric meta: label, unit, higher_is_better
    metric_defs = [
        {'key': 'avg_adherence', 'label': 'Avg Adherence',              'unit': '%',    'hib': True},
        {'key': 'at_risk_pct',   'label': 'Patients At Risk',           'unit': '%',    'hib': False},
        {'key': 'crit_per_100',  'label': 'Critical Alerts / 100 pts',  'unit': '',     'hib': False},
        {'key': 'open_per_100',  'label': 'Open Alerts / 100 pts',      'unit': '',     'hib': False},
        {'key': 'device_util',   'label': 'Device Utilization',         'unit': '%',    'hib': True},
    ]

    # Assign a tier class to each (location × metric) — green=best, red=worst, neutral in between
    if len(loc_metrics) >= 2:
        for m in metric_defs:
            vals = [loc['values'][m['key']] for loc in loc_metrics]
            vmax, vmin = max(vals), min(vals)
            if vmax == vmin:
                for loc in loc_metrics:
                    loc.setdefault('tiers', {})[m['key']] = ''
                continue
            for loc in loc_metrics:
                v = loc['values'][m['key']]
                if m['hib']:  # higher is better
                    if v == vmax:   tier = 'tier-best'
                    elif v == vmin: tier = 'tier-worst'
                    else:           tier = 'tier-mid'
                else:         # lower is better
                    if v == vmin:   tier = 'tier-best'
                    elif v == vmax: tier = 'tier-worst'
                    else:           tier = 'tier-mid'
                loc.setdefault('tiers', {})[m['key']] = tier
    else:
        for loc in loc_metrics:
            loc['tiers'] = {m['key']: '' for m in metric_defs}

    return render_template('parent_overview.html',
                           locations=locations, totals=totals,
                           buckets=buckets, bucket_total=bucket_total,
                           bucket_unknown=unknown_count,
                           loc_metrics=loc_metrics, metric_defs=metric_defs)


# ── Parent rollup: aggregated lists across all child locations ─────────────

@app.route('/parent/patients')
@require_login
def parent_patients():
    ids, bounce = _require_parent_rollup_scope()
    if bounce: return bounce
    if not ids:
        return render_template('parent_patients.html', patients=[], status='active',
                               locations=[], filter_location=0)
    status = request.args.get('status', 'active')
    sort = request.args.get('sort', 'cough_adh')
    direction = request.args.get('dir', 'asc')
    # Optional single-location filter — reject ids outside the admin's scope.
    filter_location = request.args.get('location', type=int) or 0
    if filter_location and filter_location in ids:
        query_ids = [filter_location]
    else:
        filter_location = 0
        query_ids = ids
    ph = ','.join('?' * len(query_ids))
    rows = q_all(f"""SELECT p.*, o.id AS loc_id, o.name AS loc_name,
                     u.first_name AS clinician_first, u.last_name AS clinician_last,
                     (SELECT COUNT(*) FROM alerts a WHERE a.patient_id = p.id AND a.resolved_at IS NULL) AS open_alerts
                     FROM patients p
                     JOIN organizations o ON o.id = p.organization_id
                     LEFT JOIN users u ON u.id = p.assigned_clinician_user_id
                     WHERE p.organization_id IN ({ph}) AND p.status = ?""",
                 tuple(query_ids) + (status,))
    augmented = _attach_modality_adherence(rows)
    # Flat, global sort across the whole list (no per-location grouping).
    sorted_rows = _sort_patients(list(augmented), sort, direction)
    # Location picker list — always show all locations in the admin's scope.
    locations = q_all(
        f"SELECT id, name FROM organizations WHERE id IN ({','.join('?' * len(ids))}) ORDER BY name",
        tuple(ids))
    return render_template('parent_patients.html', patients=sorted_rows,
                           status=status, sort=sort, direction=direction,
                           locations=locations, filter_location=filter_location)


@app.route('/parent/devices')
@require_login
def parent_devices():
    ids, bounce = _require_parent_rollup_scope()
    if bounce: return bounce
    if not ids:
        return render_template('parent_devices.html', devices=[],
                               status_filter='all', filter_location=0, locations=[],
                               kpis={'total':0,'in_use':0,'in_stock':0,'maintenance':0})
    status_filter = request.args.get('status', 'all')
    if status_filter not in ('all', 'assigned', 'unassigned'):
        status_filter = 'all'
    filter_location = request.args.get('location', type=int) or 0
    if filter_location and filter_location in ids:
        query_ids = [filter_location]
    else:
        filter_location = 0
        query_ids = ids
    ph = ','.join('?' * len(query_ids))
    base_cols = """d.*, o.id AS loc_id, o.name AS loc_name,
                   p.id AS patient_id, p.first_name, p.last_name, p.mrn,
                   da.assigned_date, da.consent_form_path"""
    if status_filter == 'assigned':
        rows = q_all(f"""SELECT {base_cols}
                         FROM devices d
                         JOIN organizations o ON o.id = d.organization_id
                         JOIN device_assignments da ON da.device_id = d.id AND da.returned_date IS NULL
                         JOIN patients p ON p.id = da.patient_id
                         WHERE d.organization_id IN ({ph})""", tuple(query_ids))
    elif status_filter == 'unassigned':
        rows = q_all(f"""SELECT d.*, o.id AS loc_id, o.name AS loc_name,
                         NULL AS patient_id, NULL AS first_name, NULL AS last_name,
                         NULL AS mrn, NULL AS assigned_date, NULL AS consent_form_path
                         FROM devices d
                         JOIN organizations o ON o.id = d.organization_id
                         WHERE d.organization_id IN ({ph})
                           AND NOT EXISTS (SELECT 1 FROM device_assignments da
                                           WHERE da.device_id = d.id AND da.returned_date IS NULL)""",
                     tuple(query_ids))
    else:
        rows = q_all(f"""SELECT {base_cols}
                         FROM devices d
                         JOIN organizations o ON o.id = d.organization_id
                         LEFT JOIN device_assignments da ON da.device_id = d.id AND da.returned_date IS NULL
                         LEFT JOIN patients p ON p.id = da.patient_id
                         WHERE d.organization_id IN ({ph})""", tuple(query_ids))
    # KPIs always reflect the whole rollup scope (not the selected filter), so
    # the admin keeps a stable frame of reference when narrowing the view.
    kpi_ph = ','.join('?' * len(ids))
    all_rows = q_all(f"SELECT status FROM devices WHERE organization_id IN ({kpi_ph})", tuple(ids))
    kpis = {
        'total': len(all_rows),
        'in_use': len([d for d in all_rows if d['status'] == 'in_use']),
        'in_stock': len([d for d in all_rows if d['status'] == 'in_stock']),
        'maintenance': len([d for d in all_rows if d['status'] == 'maintenance']),
    }
    locations = q_all(
        f"SELECT id, name FROM organizations WHERE id IN ({kpi_ph}) ORDER BY name",
        tuple(ids))
    return render_template('parent_devices.html', devices=rows,
                           status_filter=status_filter,
                           filter_location=filter_location,
                           locations=locations, kpis=kpis)


@app.route('/parent/alerts')
@require_login
def parent_alerts():
    ids, bounce = _require_parent_rollup_scope()
    if bounce: return bounce
    if not ids:
        return render_template('parent_alerts.html', alerts=[], filter_sev='all',
                               show='active', filter_location=0, locations=[],
                               counts={'active':0,'critical':0,'warning':0,'acknowledged':0})
    filter_sev = request.args.get('severity', 'all')
    show = request.args.get('show', 'active')
    filter_location = request.args.get('location', type=int) or 0
    if filter_location and filter_location in ids:
        query_ids = [filter_location]
    else:
        filter_location = 0
        query_ids = ids
    ph = ','.join('?' * len(query_ids))
    query = f"""SELECT a.*, o.id AS loc_id, o.name AS loc_name,
                p.first_name, p.last_name, p.mrn, p.id AS patient_id,
                r.name AS rule_name
                FROM alerts a
                JOIN organizations o ON o.id = a.organization_id
                JOIN patients p ON p.id = a.patient_id
                LEFT JOIN alert_rules r ON r.id = a.rule_id
                WHERE a.organization_id IN ({ph})"""
    params = list(query_ids)
    if show == 'active':
        query += ' AND a.resolved_at IS NULL'
    if filter_sev in ('info', 'warning', 'critical'):
        query += ' AND a.severity = ?'
        params.append(filter_sev)
    query += " ORDER BY a.triggered_at DESC"
    rows = q_all(query, tuple(params))
    # Counts always reflect the full rollup scope so filter narrowing doesn't
    # collapse the KPIs displayed in the subtitle.
    kpi_ph = ','.join('?' * len(ids))
    counts = q_one(
        f"""SELECT
        SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS active,
        SUM(CASE WHEN resolved_at IS NULL AND severity='critical' THEN 1 ELSE 0 END) AS critical,
        SUM(CASE WHEN resolved_at IS NULL AND severity='warning' THEN 1 ELSE 0 END) AS warning,
        SUM(CASE WHEN acknowledged_at IS NOT NULL AND resolved_at IS NULL THEN 1 ELSE 0 END) AS acknowledged
        FROM alerts WHERE organization_id IN ({kpi_ph})""", tuple(ids))
    locations = q_all(
        f"SELECT id, name FROM organizations WHERE id IN ({kpi_ph}) ORDER BY name",
        tuple(ids))
    return render_template('parent_alerts.html', alerts=rows, filter_sev=filter_sev,
                           show=show, counts=counts,
                           filter_location=filter_location, locations=locations)


# ── Parent admin: location management ──────────────────────────────────────

def _require_parent_admin():
    u = current_user()
    if not is_parent_admin():
        flash('Only the parent group admin can manage locations.', 'error')
        return redirect(url_for('home'))
    return None


@app.route('/parent/locations/new', methods=['GET', 'POST'])
@require_login
def parent_location_new():
    bounce = _require_parent_admin()
    if bounce: return bounce
    u = current_user()
    parent_id = u['organization_id']
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Location name is required.', 'error')
            return redirect(url_for('parent_location_new'))
        q_exec("""INSERT INTO organizations
                  (name, parent_id, type, address_line1, address_line2, city, state,
                   zip, phone, email, timezone)
                  VALUES (?, ?, 'location', ?, ?, ?, ?, ?, ?, ?, ?)""",
               (name, parent_id,
                request.form.get('address_line1') or None,
                request.form.get('address_line2') or None,
                request.form.get('city') or None,
                request.form.get('state') or None,
                request.form.get('zip') or None,
                request.form.get('phone') or None,
                request.form.get('email') or None,
                request.form.get('timezone') or 'America/New_York'))
        new_id = q_one('SELECT last_insert_rowid() AS id')['id']
        _log_access('location_create', ref_type='organization', ref_id=new_id,
                    detail=f'Created location: {name}')
        flash(f'Location "{name}" added.', 'success')
        return redirect(url_for('parent_overview'))
    return render_template('parent_location_form.html', location=None)


@app.route('/parent/locations/<int:org_id>/edit', methods=['GET', 'POST'])
@require_login
def parent_location_edit(org_id):
    bounce = _require_parent_admin()
    if bounce: return bounce
    u = current_user()
    loc = q_one("""SELECT * FROM organizations
                   WHERE id = ? AND parent_id = ? AND type = 'location'""",
                (org_id, u['organization_id']))
    if not loc: abort(404)
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Location name is required.', 'error')
            return redirect(url_for('parent_location_edit', org_id=org_id))
        q_exec("""UPDATE organizations SET name = ?, address_line1 = ?,
                  address_line2 = ?, city = ?, state = ?, zip = ?, phone = ?,
                  email = ?, timezone = ? WHERE id = ?""",
               (name,
                request.form.get('address_line1') or None,
                request.form.get('address_line2') or None,
                request.form.get('city') or None,
                request.form.get('state') or None,
                request.form.get('zip') or None,
                request.form.get('phone') or None,
                request.form.get('email') or None,
                request.form.get('timezone') or 'America/New_York',
                org_id))
        _log_access('location_update', ref_type='organization', ref_id=org_id,
                    detail=f'Updated location: {name}')
        flash(f'Location "{name}" updated.', 'success')
        return redirect(url_for('parent_overview'))
    return render_template('parent_location_form.html', location=loc)


# ── ABMRC Super Admin ──────────────────────────────────────────────────────

ALLOWED_BAA_EXT = {'pdf'}
BAA_DIR = Path(app.root_path) / 'static' / 'uploads' / 'baas'


def _require_super_admin():
    if not is_super_admin():
        flash('Super admin access required.', 'error')
        return redirect(url_for('home'))
    return None


def _baa_status(baa_row, today=None):
    """Return ('active'|'expiring'|'expired'|'revoked'|'none', days_left)."""
    from datetime import date as _d
    today = today or _d.today()
    if baa_row is None: return ('none', None)
    if baa_row['revoked_at']: return ('revoked', None)
    if not baa_row['expires_on']: return ('active', None)
    expires = _d.fromisoformat(baa_row['expires_on'])
    days = (expires - today).days
    if days < 0: return ('expired', days)
    if days <= 30: return ('expiring', days)
    return ('active', days)


@app.route('/super')
@require_login
def super_dashboard():
    bounce = _require_super_admin()
    if bounce: return bounce
    # Customer organizations = parent orgs + standalone single-org locations
    # (orgs with type != 'internal'). Each gets its latest non-revoked BAA.
    orgs = q_all("""SELECT o.*,
                    (SELECT COUNT(*) FROM patients p
                       JOIN organizations co ON co.id = p.organization_id
                       WHERE co.id = o.id OR co.parent_id = o.id) AS patient_count,
                    (SELECT COUNT(*) FROM devices d
                       JOIN organizations co ON co.id = d.organization_id
                       WHERE co.id = o.id OR co.parent_id = o.id) AS device_count,
                    (SELECT COUNT(*) FROM alerts a
                       JOIN organizations co ON co.id = a.organization_id
                       WHERE (co.id = o.id OR co.parent_id = o.id)
                         AND a.resolved_at IS NULL) AS active_alerts
                    FROM organizations o
                    WHERE o.type IN ('parent', 'location') AND o.parent_id IS NULL
                    ORDER BY o.name""")
    rows = []
    for o in orgs:
        baa = q_one("""SELECT * FROM organization_baas
                       WHERE organization_id = ? AND revoked_at IS NULL
                       ORDER BY effective_from DESC LIMIT 1""", (o['id'],))
        baa_state, days_left = _baa_status(baa)
        rows.append({**dict(o), 'baa': dict(baa) if baa else None,
                     'baa_state': baa_state, 'baa_days_left': days_left})
    counts = q_one("""SELECT
                        SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                        SUM(CASE WHEN status = 'pending_setup' THEN 1 ELSE 0 END) AS pending
                      FROM organizations
                      WHERE type IN ('parent','location')""")
    totals = q_one("""SELECT
                        (SELECT COUNT(*) FROM patients WHERE status = 'active') AS patients,
                        (SELECT COUNT(*) FROM devices  WHERE status != 'retired') AS devices,
                        (SELECT COUNT(*) FROM alerts   WHERE resolved_at IS NULL) AS alerts""")
    return render_template('super_dashboard.html', orgs=rows,
                           active_count=counts['active'] or 0,
                           pending_count=counts['pending'] or 0,
                           total_patients=totals['patients'] or 0,
                           total_devices=totals['devices'] or 0,
                           total_alerts=totals['alerts'] or 0)


@app.route('/super/orgs/new', methods=['GET', 'POST'])
@require_login
def super_org_new():
    bounce = _require_super_admin()
    if bounce: return bounce
    if request.method == 'POST':
        # Org name + parent admin invite are the only hard requirements.
        org_name = (request.form.get('name') or '').strip()
        if not org_name:
            flash('Organization name is required.', 'error')
            return redirect(url_for('super_org_new'))
        invite_email = (request.form.get('invite_email') or '').strip().lower()
        invite_first = (request.form.get('invite_first_name') or '').strip()
        invite_last = (request.form.get('invite_last_name') or '').strip()
        if not (invite_email and invite_first and invite_last):
            flash('An initial group admin invite (email + name) is required.', 'error')
            return redirect(url_for('super_org_new'))

        # BAA is optional at creation. If a file is provided, validate it
        # and the signed date; effective_from defaults to signed_date and
        # expires_on is left NULL. The super_org_activate route refuses to
        # flip the org to 'active' until a BAA exists.
        baa_file = request.files.get('baa_document')
        baa_provided = bool(baa_file and baa_file.filename)
        signed_date = request.form.get('signed_date')
        if baa_provided:
            ext = baa_file.filename.rsplit('.', 1)[-1].lower() if '.' in baa_file.filename else ''
            if ext not in ALLOWED_BAA_EXT:
                flash('BAA upload must be a PDF.', 'error')
                return redirect(url_for('super_org_new'))
            if not signed_date:
                flash('BAA signed date is required when a BAA is uploaded.', 'error')
                return redirect(url_for('super_org_new'))

        u = current_user()
        verification_date = request.form.get('verification_date') or None
        verification_complete = 1 if verification_date else 0

        q_exec("""INSERT INTO organizations (name, parent_id, type, status,
                  address_line1, address_line2, city, state, zip, phone, npi,
                  verification_complete, verification_date, verification_notes,
                  verified_by_user_id)
                  VALUES (?, NULL, 'parent', 'pending_setup', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (org_name,
                request.form.get('address_line1') or None,
                request.form.get('address_line2') or None,
                request.form.get('city') or None,
                request.form.get('state') or None,
                request.form.get('zip') or None,
                request.form.get('phone') or None,
                request.form.get('npi') or None,
                verification_complete,
                verification_date,
                request.form.get('verification_notes') or None,
                u['id']))
        new_org_id = q_one('SELECT last_insert_rowid() AS id')['id']

        if baa_provided:
            BAA_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            safe_name = secure_filename(f'org{new_org_id}_baa_{ts}.{ext}')
            baa_file.save(BAA_DIR / safe_name)
            q_exec("""INSERT INTO organization_baas (organization_id, file_path, file_name,
                      signed_date, effective_from, expires_on,
                      signed_by_name, signed_by_title, uploaded_by_user_id)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (new_org_id, f'uploads/baas/{safe_name}',
                    secure_filename(baa_file.filename),
                    signed_date, signed_date, None,
                    request.form.get('signed_by_name') or None,
                    request.form.get('signed_by_title') or None, u['id']))

        # Parent-admin invite
        import secrets
        from datetime import timedelta as _td
        token = secrets.token_urlsafe(24)
        expires_at = (datetime.now() + _td(days=14)).isoformat(sep=' ', timespec='seconds')
        q_exec("""INSERT INTO org_invitations (organization_id, email, first_name,
                  last_name, token, invited_by_user_id, expires_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?)""",
               (new_org_id, invite_email, invite_first, invite_last, token,
                u['id'], expires_at))

        _log_access('location_create', ref_type='organization', ref_id=new_org_id,
                    detail=f'Super admin created customer org "{org_name}"; invited {invite_email}; BAA={"on file" if baa_provided else "missing"}')
        if baa_provided:
            flash(f'Organization "{org_name}" created. Invite sent to {invite_email}. Account is in pending setup until activated.', 'success')
        else:
            flash(f'Organization "{org_name}" created without a BAA. Invite sent to {invite_email}. Upload a signed BAA to enable activation.', 'success')
        return redirect(url_for('super_dashboard'))
    return render_template('super_org_form.html')


@app.route('/super/orgs/<int:org_id>/access', methods=['POST'])
@require_login
def super_set_acting_org(org_id):
    bounce = _require_super_admin()
    if bounce: return bounce
    target = q_one("""SELECT * FROM organizations
                      WHERE id = ? AND type IN ('parent','location') AND parent_id IS NULL""",
                   (org_id,))
    if not target:
        flash('Customer organization not found.', 'error')
        return redirect(url_for('super_dashboard'))
    if target['status'] != 'active':
        flash('Cannot access a suspended or pending organization.', 'error')
        return redirect(url_for('super_dashboard'))
    # Require an active, non-revoked, non-expired BAA before any data access.
    baa = q_one("""SELECT * FROM organization_baas
                   WHERE organization_id = ? AND revoked_at IS NULL
                   ORDER BY effective_from DESC LIMIT 1""", (org_id,))
    state, _ = _baa_status(baa)
    if state not in ('active', 'expiring'):
        flash('No active BAA on file — access blocked.', 'error')
        return redirect(url_for('super_dashboard'))
    session['super_acting_org_id'] = org_id
    justification = (request.form.get('justification') or '').strip() or 'general support access'
    _log_access('patient_view', ref_type='organization', ref_id=org_id,
                detail=f'Super admin opened {target["name"]}: {justification}')
    # Standalone orgs have no rollup; route them to /dashboard.
    if target['type'] == 'parent':
        return redirect(url_for('parent_overview'))
    return redirect(url_for('dashboard'))


@app.route('/super/exit', methods=['POST'])
@require_login
def super_clear_acting_org(*_):
    bounce = _require_super_admin()
    if bounce: return bounce
    session.pop('super_acting_org_id', None)
    return redirect(url_for('super_dashboard'))


@app.route('/super/orgs/<int:org_id>/suspend', methods=['POST'])
@require_login
def super_org_suspend(org_id):
    bounce = _require_super_admin()
    if bounce: return bounce
    q_exec("UPDATE organizations SET status = 'suspended' WHERE id = ?", (org_id,))
    _log_access('location_update', ref_type='organization', ref_id=org_id,
                detail='Org suspended by super admin')
    flash('Organization suspended.', 'success')
    return redirect(url_for('super_dashboard'))


@app.route('/super/orgs/<int:org_id>/activate', methods=['POST'])
@require_login
def super_org_activate(org_id):
    bounce = _require_super_admin()
    if bounce: return bounce
    has_baa = q_one(
        'SELECT 1 AS x FROM organization_baas WHERE organization_id = ? LIMIT 1',
        (org_id,))
    if not has_baa:
        flash('Cannot activate: a signed BAA must be on file before this organization can be active.', 'error')
        return redirect(url_for('super_dashboard'))
    org = q_one('SELECT verification_complete FROM organizations WHERE id = ?', (org_id,))
    if not (org and org['verification_complete']):
        flash('Cannot activate: account verification (verification date) must be recorded before this organization can be active.', 'error')
        return redirect(url_for('super_dashboard'))
    q_exec("UPDATE organizations SET status = 'active' WHERE id = ?", (org_id,))
    _log_access('location_update', ref_type='organization', ref_id=org_id,
                detail='Org activated by super admin')
    flash('Organization activated.', 'success')
    return redirect(url_for('super_dashboard'))


@app.route('/super/orgs/<int:org_id>/update', methods=['GET', 'POST'])
@require_login
def super_org_update(org_id):
    bounce = _require_super_admin()
    if bounce: return bounce
    org = q_one('SELECT * FROM organizations WHERE id = ?', (org_id,))
    if not org:
        abort(404)
    current_baa = q_one(
        """SELECT * FROM organization_baas
           WHERE organization_id = ?
           ORDER BY id DESC LIMIT 1""", (org_id,))

    if request.method == 'POST':
        org_name = (request.form.get('name') or '').strip()
        if not org_name:
            flash('Organization name is required.', 'error')
            return redirect(url_for('super_org_update', org_id=org_id))

        baa_file = request.files.get('baa_document')
        baa_provided = bool(baa_file and baa_file.filename)
        signed_date = request.form.get('signed_date')
        if baa_provided:
            ext = baa_file.filename.rsplit('.', 1)[-1].lower() if '.' in baa_file.filename else ''
            if ext not in ALLOWED_BAA_EXT:
                flash('BAA upload must be a PDF.', 'error')
                return redirect(url_for('super_org_update', org_id=org_id))
            if not signed_date:
                flash('BAA signed date is required when a BAA is uploaded.', 'error')
                return redirect(url_for('super_org_update', org_id=org_id))

        u = current_user()
        verification_date = request.form.get('verification_date') or None
        verification_complete = 1 if verification_date else 0

        q_exec("""UPDATE organizations SET
                  name = ?, address_line1 = ?, address_line2 = ?,
                  city = ?, state = ?, zip = ?, phone = ?, npi = ?,
                  verification_complete = ?, verification_date = ?,
                  verification_notes = ?, verified_by_user_id = ?
                  WHERE id = ?""",
               (org_name,
                request.form.get('address_line1') or None,
                request.form.get('address_line2') or None,
                request.form.get('city') or None,
                request.form.get('state') or None,
                request.form.get('zip') or None,
                request.form.get('phone') or None,
                request.form.get('npi') or None,
                verification_complete,
                verification_date,
                request.form.get('verification_notes') or None,
                u['id'], org_id))

        if baa_provided:
            BAA_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            safe_name = secure_filename(f'org{org_id}_baa_{ts}.{ext}')
            baa_file.save(BAA_DIR / safe_name)
            q_exec("""INSERT INTO organization_baas (organization_id, file_path, file_name,
                      signed_date, effective_from, expires_on,
                      signed_by_name, signed_by_title, uploaded_by_user_id)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (org_id, f'uploads/baas/{safe_name}',
                    secure_filename(baa_file.filename),
                    signed_date, signed_date, None,
                    request.form.get('signed_by_name') or None,
                    request.form.get('signed_by_title') or None, u['id']))

        _log_access('location_update', ref_type='organization', ref_id=org_id,
                    detail=f'Super admin updated org "{org_name}"; BAA={"new upload" if baa_provided else "unchanged"}')
        flash(f'"{org_name}" updated.', 'success')
        return redirect(url_for('super_dashboard'))

    verified_by_name = None
    if org['verified_by_user_id']:
        v = q_one('SELECT first_name, last_name FROM users WHERE id = ?',
                  (org['verified_by_user_id'],))
        if v:
            verified_by_name = f"{v['first_name']} {v['last_name']}"

    return render_template('super_org_update_form.html',
                           org=org, current_baa=current_baa,
                           verified_by_name=verified_by_name)


# Two-letter US state code → FIPS numeric (matches us-atlas topojson state ids).
_US_STATE_FIPS = {
    'AL':'01','AK':'02','AZ':'04','AR':'05','CA':'06','CO':'08','CT':'09','DE':'10',
    'DC':'11','FL':'12','GA':'13','HI':'15','ID':'16','IL':'17','IN':'18','IA':'19',
    'KS':'20','KY':'21','LA':'22','ME':'23','MD':'24','MA':'25','MI':'26','MN':'27',
    'MS':'28','MO':'29','MT':'30','NE':'31','NV':'32','NH':'33','NJ':'34','NM':'35',
    'NY':'36','NC':'37','ND':'38','OH':'39','OK':'40','OR':'41','PA':'42','RI':'44',
    'SC':'45','SD':'46','TN':'47','TX':'48','UT':'49','VT':'50','VA':'51','WA':'53',
    'WV':'54','WI':'55','WY':'56','PR':'72'
}


@app.route('/super/map')
@require_login
def super_map():
    bounce = _require_super_admin()
    if bounce: return bounce
    metric = (request.args.get('metric') or 'patients').lower()
    if metric not in ('patients', 'devices', 'alerts'):
        metric = 'patients'

    # Aggregate counts by the location's state. For each org_id, the patient/
    # device/alert is attributed to that org's state.
    if metric == 'patients':
        rows = q_all("""SELECT o.state AS state, COUNT(*) AS n
                        FROM patients p JOIN organizations o ON o.id = p.organization_id
                        WHERE p.status = 'active' AND o.state IS NOT NULL
                        GROUP BY o.state""")
        label = 'Active patients'
    elif metric == 'devices':
        rows = q_all("""SELECT o.state AS state, COUNT(*) AS n
                        FROM devices d JOIN organizations o ON o.id = d.organization_id
                        WHERE d.status != 'retired' AND o.state IS NOT NULL
                        GROUP BY o.state""")
        label = 'Active devices'
    else:
        rows = q_all("""SELECT o.state AS state, COUNT(*) AS n
                        FROM alerts a JOIN organizations o ON o.id = a.organization_id
                        WHERE a.resolved_at IS NULL AND o.state IS NOT NULL
                        GROUP BY o.state""")
        label = 'Active alerts'

    # Map state code → FIPS, build {fips: count} for the front-end.
    by_fips = {}
    by_state = {}
    for r in rows:
        code = (r['state'] or '').upper()
        fips = _US_STATE_FIPS.get(code)
        if fips:
            by_fips[fips] = r['n']
            by_state[code] = r['n']

    total = sum(by_fips.values())
    return render_template('super_map.html', metric=metric, metric_label=label,
                           by_fips=by_fips, by_state=by_state, total=total)


# ── Population health heat map (group + satellite admins) ───────────────────
#
# A ZIP-3 proportional-symbol map with a 12-month timelapse. Per-org admins
# pick which layers (metrics) to expose under Settings → Heat map layers.
# Tenant scoping reuses scope_org_ids() so:
#   • Group admin at rollup → all satellite locations
#   • Satellite admin       → only their satellite
#   • ABMRC drilled into a customer → that customer's tree, de-identified
# ZIP-3 cells with patient_count below the user-selected suppression floor
# are dropped from the response (HIPAA Safe Harbor pattern; floor defaults
# to 1 in the demo so synthetic data is visible, with a UI toggle to apply
# the standard ≥11 threshold).

HEATMAP_LAYERS = [
    {'key': 'active_patients',   'label': 'Active patients',
     'unit': 'patients',  'lower_is_better': False,
     'desc': 'Active patients in each region. Volume map.'},
    {'key': 'adherence',         'label': 'Avg 30-day adherence',
     'unit': '%',         'lower_is_better': False,
     'desc': 'Average therapy adherence across active patients in the region.'},
    {'key': 'alerts',            'label': 'Active alerts',
     'unit': 'alerts',    'lower_is_better': True,
     'desc': 'Open (unresolved) alerts across all severities.'},
    {'key': 'critical_alerts',   'label': 'Critical alerts',
     'unit': 'alerts',    'lower_is_better': True,
     'desc': 'Open alerts with severity = critical.'},
    {'key': 'referrals',         'label': 'New referrals this month',
     'unit': 'patients',  'lower_is_better': False,
     'desc': 'Patients newly assigned to a referring clinic during the selected month.'},
    {'key': 'referring_clinics', 'label': 'Active referring clinics',
     'unit': 'clinics',   'lower_is_better': False,
     'desc': 'Distinct referring clinics that sent at least one patient during the selected month.'},
    {'key': 'capped_rental_late','label': 'Capped-rental month 10–13',
     'unit': 'patients',  'lower_is_better': False,
     'desc': 'Patients in late capped-rental window (Medicare DME conversion zone).'},
    {'key': 'high_risk',         'label': 'High-risk composite',
     'unit': 'patients',  'lower_is_better': True,
     'desc': 'Patients with adherence <60% AND an open alert during the selected month.'},
    {'key': 'pro_score',         'label': 'Patient survey score (avg)',
     'unit': '0–100',     'lower_is_better': False,
     'desc': 'Average score from completed 30/60/90-day patient surveys (higher = better outcomes).'},
    {'key': 'discontinuation',   'label': 'Discontinuations this month',
     'unit': 'patients',  'lower_is_better': True,
     'desc': 'Patients who became inactive during the selected month.'},
    {'key': 'mobile_adoption',   'label': 'Mobile-app adoption',
     'unit': '%',         'lower_is_better': False,
     'desc': 'Share of active patients with the Arc Connect mobile app paired.'},
]
DEFAULT_HEATMAP_LAYER_KEYS = [l['key'] for l in HEATMAP_LAYERS]
HEATMAP_LAYER_BY_KEY = {l['key']: l for l in HEATMAP_LAYERS}


def _load_zip3_centroids():
    """Cached load of static/vendor/zip3-centroids.json. Falls back to empty
    dict if the file is missing — heat map then relies entirely on org lat/lon."""
    if not hasattr(_load_zip3_centroids, '_cache'):
        try:
            path = HERE / 'static' / 'vendor' / 'zip3-centroids.json'
            data = json.loads(path.read_text())
            data.pop('_comment', None)
            _load_zip3_centroids._cache = data
        except Exception as e:
            app.logger.warning(f'zip3 centroids load failed: {e}')
            _load_zip3_centroids._cache = {}
    return _load_zip3_centroids._cache


def _enabled_heatmap_layers(oid):
    """Layer keys the org has enabled. Absent row = all defaults."""
    row = q_one('SELECT layers_json FROM heatmap_settings WHERE organization_id = ?',
                (oid,))
    if not row:
        return list(DEFAULT_HEATMAP_LAYER_KEYS)
    try:
        keys = json.loads(row['layers_json'])
        return [k for k in keys if k in HEATMAP_LAYER_BY_KEY]
    except Exception:
        return list(DEFAULT_HEATMAP_LAYER_KEYS)


def _heatmap_settings_org_id():
    """Which org's heatmap_settings row applies to the current viewer.
    Group admin at rollup → parent org; satellite admin → satellite org;
    super admin drilled into a customer parent → that parent. Falls back
    to current_org_id() for anyone else."""
    if is_parent_admin() and not session.get('acting_org_id'):
        u = current_user()
        return u['organization_id'] if u else None
    if is_super_admin() and session.get('super_acting_org_id'):
        return session['super_acting_org_id']
    return current_org_id()


def _heatmap_time_bins(months_back=12):
    """Return the list of monthly bins (oldest first) up to current month.
    Each bin: {'key': 'YYYY-MM', 'label': 'Mon YYYY', 'start': iso, 'end': iso_exclusive}.
    """
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    # Anchor on first-of-current-month
    cur = _date(today.year, today.month, 1)
    bins = []
    for i in range(months_back - 1, -1, -1):
        # i months back from cur
        m = cur.month - i
        y = cur.year
        while m <= 0:
            m += 12
            y -= 1
        start = _date(y, m, 1)
        # End = start of next month (exclusive)
        em = m + 1
        ey = y
        if em > 12:
            em = 1
            ey += 1
        end = _date(ey, em, 1)
        labels = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
        bins.append({
            'key':   f'{y:04d}-{m:02d}',
            'label': f'{labels[m-1]} {y}',
            'start': start.isoformat(),
            'end':   end.isoformat(),
        })
    return bins


@app.route('/heatmap')
@require_login
def heatmap():
    """Population-health heat map — visible to admin + clinician roles.
    Tenant scope follows scope_org_ids(); ABMRC users not drilled into a
    customer get bounced to /super/map (they have a state-level view there)."""
    u = current_user()
    if not u:
        return redirect(url_for('login'))
    if u['role'] not in ('admin', 'clinician', 'super_admin'):
        flash('Heat map requires an admin or clinician account.', 'error')
        return redirect(url_for('dashboard'))
    if is_super_admin() and not session.get('super_acting_org_id'):
        # Super admin at their own dashboard — direct them to the state map.
        return redirect(url_for('super_map'))

    settings_oid = _heatmap_settings_org_id()
    enabled_keys = _enabled_heatmap_layers(settings_oid) if settings_oid else list(DEFAULT_HEATMAP_LAYER_KEYS)
    enabled_layers = [HEATMAP_LAYER_BY_KEY[k] for k in enabled_keys
                      if k in HEATMAP_LAYER_BY_KEY]
    if not enabled_layers:
        # Defensive: never render an empty layer picker.
        enabled_layers = [HEATMAP_LAYER_BY_KEY[DEFAULT_HEATMAP_LAYER_KEYS[0]]]

    # Scope label for the page header
    if is_parent_admin() and not session.get('acting_org_id'):
        scope_label = 'Network rollup — all satellite locations'
    elif is_super_admin() and session.get('super_acting_org_id'):
        scope_label = 'ABMRC support view — de-identified'
    else:
        org = current_org()
        scope_label = f'{org["name"]}' if org else ''

    return render_template('heatmap.html',
                           layers=enabled_layers,
                           default_layer=enabled_layers[0]['key'],
                           scope_label=scope_label)


@app.route('/api/heatmap-data')
@require_login
def api_heatmap_data():
    """JSON payload for the heat map. Query params:
        metric=<layer key>     which layer to compute (defaults to active_patients)
        floor=<int>            small-cell suppression threshold (default 1; 11 for HIPAA)
    Returns 12 monthly bins; each bin maps ZIP-3 → {value, n}.
    """
    u = current_user()
    if not u:
        return jsonify({'error': 'unauthenticated'}), 401
    if u['role'] not in ('admin', 'clinician', 'super_admin'):
        return jsonify({'error': 'forbidden'}), 403

    metric = (request.args.get('metric') or 'active_patients').strip()
    if metric not in HEATMAP_LAYER_BY_KEY:
        metric = 'active_patients'
    layer = HEATMAP_LAYER_BY_KEY[metric]

    try:
        floor = max(1, int(request.args.get('floor', 1)))
    except (TypeError, ValueError):
        floor = 1

    org_ids = scope_org_ids()
    if not org_ids:
        return jsonify({'metric': metric, 'metric_label': layer['label'],
                        'unit': layer['unit'], 'lower_is_better': layer['lower_is_better'],
                        'floor': floor, 'bins': []})
    ph_org = ','.join('?' * len(org_ids))

    centroids = _load_zip3_centroids()
    bins = _heatmap_time_bins(12)

    # Pull org centroid fallbacks (so patients with unknown ZIP-3 still pin somewhere).
    org_loc_rows = q_all(
        f"""SELECT id, latitude, longitude, name, zip
            FROM organizations WHERE id IN ({ph_org})""", tuple(org_ids))
    org_locs = {r['id']: dict(r) for r in org_loc_rows}

    # Helper: produce the ZIP-3 key for a patient's address. Falls back to
    # the org's ZIP-3 if patient ZIP is missing.
    def zip3_for(patient_zip, org_id):
        z = (patient_zip or '').strip()
        if len(z) >= 3 and z[:3].isdigit():
            return z[:3]
        ozip = (org_locs.get(org_id, {}).get('zip') or '').strip()
        if len(ozip) >= 3 and ozip[:3].isdigit():
            return ozip[:3]
        return None

    # Pre-fetch all patients (id, org_id, zip, status, adherence, capped_rental_*,
    # mobile_app_enabled, created_at). Used by every metric.
    patient_rows = q_all(
        f"""SELECT id, organization_id, zip, status, adherence_pct_30d,
                   capped_rental_start, capped_rental_end, mobile_app_enabled,
                   created_at
            FROM patients WHERE organization_id IN ({ph_org})""", tuple(org_ids))
    # Index by id for fast joins.
    pat_by_id = {r['id']: dict(r) for r in patient_rows}
    pat_zip3 = {pid: zip3_for(p['zip'], p['organization_id']) for pid, p in pat_by_id.items()}

    # Per ZIP-3 patient denominator (active patients) for suppression floor.
    # Computed once (uses current state); applied to every bin.
    z3_active_n = {}
    for pid, p in pat_by_id.items():
        if p['status'] != 'active': continue
        z = pat_zip3.get(pid)
        if not z: continue
        z3_active_n[z] = z3_active_n.get(z, 0) + 1

    bin_payloads = []

    # Per-metric aggregation. Each branch produces a {zip3 → raw value} dict for
    # each bin. We then attach n (active patient count) for suppression.

    if metric == 'active_patients':
        # Snapshot at end of bin: count patients created on/before bin end and
        # not yet inactive at that time. Status transitions aren't logged, so
        # for the demo we approximate "active at end" with current status.
        for b in bins:
            z3_value = {}
            for pid, p in pat_by_id.items():
                if (p['created_at'] or '')[:10] >= b['end']:
                    continue
                if p['status'] != 'active':
                    continue
                z = pat_zip3.get(pid)
                if not z: continue
                z3_value[z] = z3_value.get(z, 0) + 1
            bin_payloads.append((b, z3_value))

    elif metric == 'adherence':
        # Snapshot avg adherence (current value, replicated across bins for
        # demo purposes — adherence history isn't time-stamped in v1).
        z3_sum = {}
        z3_n = {}
        for pid, p in pat_by_id.items():
            if p['status'] != 'active' or p['adherence_pct_30d'] is None:
                continue
            z = pat_zip3.get(pid)
            if not z: continue
            z3_sum[z] = z3_sum.get(z, 0) + p['adherence_pct_30d']
            z3_n[z]   = z3_n.get(z, 0) + 1
        z3_value = {z: round(z3_sum[z] / z3_n[z]) for z in z3_sum if z3_n[z]}
        for b in bins:
            bin_payloads.append((b, z3_value))

    elif metric in ('alerts', 'critical_alerts'):
        sev_clause = " AND a.severity = 'critical'" if metric == 'critical_alerts' else ""
        rows = q_all(
            f"""SELECT a.patient_id, a.triggered_at, a.resolved_at
                FROM alerts a
                WHERE a.organization_id IN ({ph_org}){sev_clause}""", tuple(org_ids))
        for b in bins:
            z3_value = {}
            for r in rows:
                trig = (r['triggered_at'] or '')[:10]
                resv = (r['resolved_at'] or '')[:10] or None
                # Open during the bin if triggered before bin_end and unresolved by bin_start.
                if trig >= b['end']:
                    continue
                if resv and resv < b['start']:
                    continue
                p = pat_by_id.get(r['patient_id'])
                if not p: continue
                z = pat_zip3.get(r['patient_id'])
                if not z: continue
                z3_value[z] = z3_value.get(z, 0) + 1
            bin_payloads.append((b, z3_value))

    elif metric == 'referrals':
        rows = q_all(
            f"""SELECT patient_id, assigned_at FROM patient_referral_history
                WHERE organization_id IN ({ph_org})""", tuple(org_ids))
        for b in bins:
            z3_value = {}
            for r in rows:
                d = (r['assigned_at'] or '')[:10]
                if not (b['start'] <= d < b['end']):
                    continue
                z = pat_zip3.get(r['patient_id'])
                if not z: continue
                z3_value[z] = z3_value.get(z, 0) + 1
            bin_payloads.append((b, z3_value))

    elif metric == 'referring_clinics':
        rows = q_all(
            f"""SELECT patient_id, clinic_id, assigned_at
                FROM patient_referral_history
                WHERE organization_id IN ({ph_org}) AND clinic_id IS NOT NULL""",
            tuple(org_ids))
        for b in bins:
            z3_clinics = {}  # zip3 → set of clinic_ids
            for r in rows:
                d = (r['assigned_at'] or '')[:10]
                if not (b['start'] <= d < b['end']):
                    continue
                z = pat_zip3.get(r['patient_id'])
                if not z: continue
                z3_clinics.setdefault(z, set()).add(r['clinic_id'])
            z3_value = {z: len(s) for z, s in z3_clinics.items()}
            bin_payloads.append((b, z3_value))

    elif metric == 'capped_rental_late':
        # Snapshot: patients whose capped_rental_end is within ~90 days of now
        # (months 10-13 of a 13-month rental). Replicated across bins for now.
        from datetime import date as _date
        today = _date.today()
        z3_value = {}
        for pid, p in pat_by_id.items():
            if not p['capped_rental_end']:
                continue
            try:
                end_d = _date.fromisoformat(p['capped_rental_end'])
            except Exception:
                continue
            days_left = (end_d - today).days
            if not (-30 <= days_left <= 120):
                continue
            z = pat_zip3.get(pid)
            if not z: continue
            z3_value[z] = z3_value.get(z, 0) + 1
        for b in bins:
            bin_payloads.append((b, z3_value))

    elif metric == 'high_risk':
        # adherence < 60 AND has an open alert in the bin
        alert_rows = q_all(
            f"""SELECT patient_id, triggered_at, resolved_at FROM alerts
                WHERE organization_id IN ({ph_org})""", tuple(org_ids))
        for b in bins:
            patients_with_alert = set()
            for r in alert_rows:
                trig = (r['triggered_at'] or '')[:10]
                resv = (r['resolved_at'] or '')[:10] or None
                if trig >= b['end']: continue
                if resv and resv < b['start']: continue
                patients_with_alert.add(r['patient_id'])
            z3_value = {}
            for pid in patients_with_alert:
                p = pat_by_id.get(pid)
                if not p: continue
                if p['adherence_pct_30d'] is None or p['adherence_pct_30d'] >= 60:
                    continue
                z = pat_zip3.get(pid)
                if not z: continue
                z3_value[z] = z3_value.get(z, 0) + 1
            bin_payloads.append((b, z3_value))

    elif metric == 'pro_score':
        rows = q_all(
            f"""SELECT patient_id, score_0_100, completed_at
                FROM patient_surveys
                WHERE organization_id IN ({ph_org})
                  AND completed_at IS NOT NULL AND score_0_100 IS NOT NULL""",
            tuple(org_ids))
        for b in bins:
            z3_sum = {}
            z3_n = {}
            for r in rows:
                d = (r['completed_at'] or '')[:10]
                if not (b['start'] <= d < b['end']):
                    continue
                z = pat_zip3.get(r['patient_id'])
                if not z: continue
                z3_sum[z] = z3_sum.get(z, 0) + r['score_0_100']
                z3_n[z]   = z3_n.get(z, 0) + 1
            z3_value = {z: round(z3_sum[z] / z3_n[z]) for z in z3_sum if z3_n[z]}
            bin_payloads.append((b, z3_value))

    elif metric == 'discontinuation':
        # We don't have a discontinuation timestamp; approximate "discontinued
        # in this bin" as inactive patients whose last_session_at falls in the
        # bin (proxy for time of drop-off). Heuristic, demo-quality.
        rows = q_all(
            f"""SELECT id, organization_id, zip, last_session_at, status
                FROM patients
                WHERE organization_id IN ({ph_org}) AND status = 'inactive'""",
            tuple(org_ids))
        for b in bins:
            z3_value = {}
            for r in rows:
                d = (r['last_session_at'] or '')[:10]
                if not d or not (b['start'] <= d < b['end']):
                    continue
                z = zip3_for(r['zip'], r['organization_id'])
                if not z: continue
                z3_value[z] = z3_value.get(z, 0) + 1
            bin_payloads.append((b, z3_value))

    elif metric == 'mobile_adoption':
        # Snapshot: % of active patients with mobile_app_enabled = 1.
        z3_n = {}
        z3_on = {}
        for pid, p in pat_by_id.items():
            if p['status'] != 'active': continue
            z = pat_zip3.get(pid)
            if not z: continue
            z3_n[z] = z3_n.get(z, 0) + 1
            if p['mobile_app_enabled']:
                z3_on[z] = z3_on.get(z, 0) + 1
        z3_value = {z: round(100 * z3_on.get(z, 0) / z3_n[z]) for z in z3_n if z3_n[z]}
        for b in bins:
            bin_payloads.append((b, z3_value))

    else:
        # Unknown layer — empty payload.
        for b in bins:
            bin_payloads.append((b, {}))

    # Build the response, applying suppression and attaching centroids.
    out_bins = []
    for b, z3_value in bin_payloads:
        zip3_out = {}
        suppressed = 0
        for z, v in z3_value.items():
            n = z3_active_n.get(z, 0)
            if n < floor:
                suppressed += 1
                continue
            cen = centroids.get(z)
            if not cen:
                # No centroid known — skip (would be invisible anyway).
                suppressed += 1
                continue
            zip3_out[z] = {'value': v, 'n': n,
                           'lat': cen['lat'], 'lon': cen['lon'],
                           'label': cen.get('label', z)}
        out_bins.append({
            'key': b['key'], 'label': b['label'],
            'start': b['start'], 'end': b['end'],
            'zip3': zip3_out,
            'suppressed_count': suppressed,
        })

    return jsonify({
        'metric': metric,
        'metric_label': layer['label'],
        'unit': layer['unit'],
        'lower_is_better': layer['lower_is_better'],
        'description': layer['desc'],
        'floor': floor,
        'bins': out_bins,
    })


@app.route('/settings/heatmap-layers', methods=['POST'])
@require_login
@require_admin
def settings_heatmap_layers():
    """Save the per-org enabled heat-map layers. Group admin's row applies
    to the network rollup; satellite admin's row applies to that satellite."""
    u = current_user()
    settings_oid = _heatmap_settings_org_id()
    if not settings_oid:
        flash('Could not determine organization scope.', 'error')
        return redirect(url_for('settings'))
    submitted = request.form.getlist('layer')
    valid = [k for k in submitted if k in HEATMAP_LAYER_BY_KEY]
    if not valid:
        # Refuse to save an empty selection — heat map would be unusable.
        flash('Pick at least one heat-map layer.', 'error')
        return redirect(url_for('settings'))
    payload = json.dumps(valid)
    q_exec("""INSERT INTO heatmap_settings
              (organization_id, layers_json, updated_at, updated_by_user_id)
              VALUES (?, ?, CURRENT_TIMESTAMP, ?)
              ON CONFLICT(organization_id) DO UPDATE SET
                layers_json = excluded.layers_json,
                updated_at  = CURRENT_TIMESTAMP,
                updated_by_user_id = excluded.updated_by_user_id""",
           (settings_oid, payload, u['id']))
    flash('Heat-map layers saved.', 'success')
    return redirect(url_for('settings'))


# ── Dashboard ────────────────────────────────────────────────────────────────

def _parent_redirect_if_no_location(endpoint):
    """If the caller is a parent admin with no acting_org_id, bounce them to the
    parent-scoped equivalent so the top nav 'just works' at rollup level."""
    if is_parent_admin() and not session.get('acting_org_id'):
        return redirect(url_for(endpoint, **request.args))
    return None


def _build_population_charts(oid):
    """Compute the five dashboard population-health chart payloads.
    Returns a dict the template renders into an SVG/map card picker."""
    from datetime import date as _date, timedelta as _td

    # ── 1. Adherence distribution (Cough + Clear, four buckets) ──────
    patients = q_all("""SELECT p.* FROM patients p
                        WHERE p.organization_id = ? AND p.status = 'active'""",
                     (oid,))
    augmented = _attach_modality_adherence(patients)
    buckets = [
        ('< 50%',   lambda v: v is not None and v < 50),
        ('50–79%',  lambda v: v is not None and 50 <= v < 80),
        ('80–99%',  lambda v: v is not None and 80 <= v < 100),
        ('100%',    lambda v: v is not None and v >= 100),
    ]
    adh_dist = []
    for label, pred in buckets:
        cough = sum(1 for p in augmented if p['has_cough'] and pred(p['cough_pct']))
        clear = sum(1 for p in augmented if p['has_clear'] and pred(p['clear_pct']))
        adh_dist.append({'label': label, 'cough': cough, 'clear': clear})

    # ── 2. Adherence trend (30-day daily average) ────────────────────
    # Daily ratio: sessions completed / (active patients × goal_per_day × modalities_count).
    today = _date.today()
    active_patient_ids = [p['id'] for p in patients]
    trend = []
    if active_patient_ids:
        placeholders = ','.join('?' * len(active_patient_ids))
        rows = q_all(f"""SELECT date(started_at) AS d, COUNT(*) AS n
                         FROM therapy_sessions
                         WHERE patient_id IN ({placeholders})
                         AND completed = 1
                         AND date(started_at) >= date(?, '-29 days')
                         GROUP BY date(started_at)""",
                     tuple(active_patient_ids) + (today.isoformat(),))
        counts = {r['d']: r['n'] for r in rows}
        # Rx modalities count averaged: use total active prescriptions / patients
        rx_count = q_one(f"""SELECT COUNT(*) AS n FROM patient_prescriptions
                             WHERE is_active=1 AND patient_id IN ({placeholders})""",
                         tuple(active_patient_ids))
        goal = max(1, rx_count['n']) * THERAPY_GOAL_PER_DAY if rx_count else THERAPY_GOAL_PER_DAY
        for i in range(30):
            day = today - _td(days=29 - i)
            completed = counts.get(day.isoformat(), 0)
            pct = int(round(100 * min(completed, goal) / goal)) if goal else 0
            trend.append({'date': day.isoformat(), 'pct': pct})
    else:
        for i in range(30):
            day = today - _td(days=29 - i)
            trend.append({'date': day.isoformat(), 'pct': 0})

    # ── 3. Onboarding funnel ─────────────────────────────────────────
    total_patients = len(patients)
    patients_with_rx = q_one("""SELECT COUNT(DISTINCT patient_id) AS n
                                FROM patient_prescriptions pp
                                JOIN patients p ON p.id = pp.patient_id
                                WHERE p.organization_id = ? AND pp.is_active = 1""",
                             (oid,))['n']
    patients_with_device = q_one("""SELECT COUNT(DISTINCT patient_id) AS n
                                    FROM device_assignments da
                                    JOIN patients p ON p.id = da.patient_id
                                    WHERE p.organization_id = ? AND da.returned_date IS NULL""",
                                 (oid,))['n']
    patients_with_session = q_one("""SELECT COUNT(DISTINCT patient_id) AS n
                                     FROM therapy_sessions ts
                                     JOIN patients p ON p.id = ts.patient_id
                                     WHERE p.organization_id = ? AND ts.completed = 1""",
                                  (oid,))['n']
    funnel = [
        {'label': 'Total patients',             'count': total_patients},
        {'label': 'Rx issued',                  'count': patients_with_rx},
        {'label': 'Device assigned',            'count': patients_with_device},
        {'label': 'First therapy session',      'count': patients_with_session},
    ]

    # ── 4. Diagnosis mix ─────────────────────────────────────────────
    dx_rows = q_all("""SELECT COALESCE(diagnosis, 'Not recorded') AS dx, COUNT(*) AS n
                       FROM patients WHERE organization_id = ? AND status = 'active'
                       GROUP BY COALESCE(diagnosis, 'Not recorded')
                       ORDER BY n DESC""", (oid,))
    diagnosis_mix = [{'label': r['dx'], 'count': r['n']} for r in dx_rows]

    # ── 5. Referring-clinic heat map ─────────────────────────────────
    # We don't store lat/lng for clinics — geocode by city using a small
    # Colorado lookup (the seed data's cities). Unknown cities fall back
    # to the org's own coordinates with a small deterministic jitter.
    CITY_COORDS = {
        'denver':    (39.7392, -104.9903),
        'aurora':    (39.7294, -104.8319),
        'englewood': (39.6478, -104.9877),
        'boulder':   (40.0150, -105.2705),
        'lakewood':  (39.7047, -105.0814),
        'littleton': (39.6133, -105.0166),
        'golden':    (39.7555, -105.2211),
        'thornton':  (39.8681, -104.9719),
    }
    org_row = q_one("SELECT latitude, longitude FROM organizations WHERE id = ?", (oid,))
    fallback = (org_row['latitude'] or 39.7392, org_row['longitude'] or -104.9903)
    clinic_rows = q_all("""SELECT rc.id, rc.name, rc.city, rc.state,
                           COUNT(DISTINCT p.id) AS patient_count,
                           AVG(p.adherence_pct_30d) AS avg_adh
                           FROM referring_clinics rc
                           LEFT JOIN patients p
                               ON p.referring_clinic_id = rc.id AND p.status = 'active'
                           WHERE rc.organization_id = ? AND rc.is_active = 1
                           GROUP BY rc.id
                           HAVING patient_count > 0
                           ORDER BY patient_count DESC""", (oid,))
    clinics = []
    for i, c in enumerate(clinic_rows):
        key = (c['city'] or '').strip().lower()
        lat, lng = CITY_COORDS.get(key, fallback)
        # Tiny deterministic jitter so two clinics in same city don't stack.
        lat += ((i * 13) % 7 - 3) * 0.004
        lng += ((i * 17) % 7 - 3) * 0.005
        clinics.append({
            'id': c['id'], 'name': c['name'],
            'city': c['city'], 'state': c['state'],
            'patient_count': c['patient_count'],
            'avg_adh': round(c['avg_adh']) if c['avg_adh'] is not None else None,
            'lat': lat, 'lng': lng,
        })

    return {
        'adh_dist': adh_dist,
        'trend': trend,
        'funnel': funnel,
        'diagnosis_mix': diagnosis_mix,
        'clinics': clinics,
        'clinic_center': fallback,
    }


@app.route('/dashboard')
@require_login
def dashboard():
    if is_rollup_scope():
        return redirect(url_for('parent_overview'))
    oid = current_org_id()
    org = current_org()
    patients = q_all("""SELECT p.*, u.first_name AS clinician_first, u.last_name AS clinician_last
                        FROM patients p
                        LEFT JOIN users u ON u.id = p.assigned_clinician_user_id
                        WHERE p.organization_id = ? AND p.status = 'active'
                        ORDER BY p.adherence_pct_30d ASC""", (oid,))
    kpis = {
        'total_patients': len([p for p in patients if p['status'] == 'active']),
        'at_risk': len([p for p in patients if (p['adherence_pct_30d'] or 0) < 80]),
        'on_track': len([p for p in patients if (p['adherence_pct_30d'] or 0) >= 80]),
        'critical': len([p for p in patients if (p['adherence_pct_30d'] or 0) < 50]),
    }
    # Recent alerts
    alerts = q_all("""SELECT a.*, p.first_name, p.last_name, p.mrn
                      FROM alerts a JOIN patients p ON p.id = a.patient_id
                      WHERE a.organization_id = ? AND a.resolved_at IS NULL
                      ORDER BY CASE a.severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                               a.triggered_at DESC LIMIT 5""", (oid,))
    # Device KPIs
    devices = q_all('SELECT * FROM devices WHERE organization_id = ?', (oid,))
    device_kpis = {
        'total': len(devices),
        'in_use': len([d for d in devices if d['status'] == 'in_use']),
        'in_stock': len([d for d in devices if d['status'] == 'in_stock']),
        'maintenance': len([d for d in devices if d['status'] == 'maintenance']),
    }
    # My open tasks (for dashboard widget)
    u = current_user()
    my_tasks = q_all("""SELECT t.id, t.title, t.status, t.priority, t.due_at,
                        p.first_name AS pt_first, p.last_name AS pt_last,
                        a.severity AS alert_severity
                        FROM tasks t
                        LEFT JOIN patients p ON p.id = t.patient_id
                        LEFT JOIN alerts a ON a.id = t.alert_id
                        WHERE t.organization_id = ? AND t.assigned_to_user_id = ?
                        AND t.status NOT IN ('completed','cancelled')
                        ORDER BY CASE t.priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                                 t.due_at ASC NULLS LAST LIMIT 5""",
                     (oid, u['id']))
    task_counts = q_one("""SELECT
        SUM(CASE WHEN assigned_to_user_id = ? AND status NOT IN ('completed','cancelled') THEN 1 ELSE 0 END) AS my_open,
        SUM(CASE WHEN assigned_to_user_id = ? AND status NOT IN ('completed','cancelled')
                 AND due_at IS NOT NULL AND due_at < datetime('now') THEN 1 ELSE 0 END) AS my_overdue
        FROM tasks WHERE organization_id = ?""", (u['id'], u['id'], oid))
    # Augment top-8 at-risk with modality adherence + alert severity — same shape
    # as the patient list so the dashboard uses the same chip component.
    top_patients = _attach_modality_adherence(patients[:8])
    pop_charts = _build_population_charts(oid)
    return render_template('dashboard.html',
                           patients=top_patients, kpis=kpis, alerts=alerts,
                           device_kpis=device_kpis, my_tasks=my_tasks,
                           task_counts=task_counts,
                           status_labels=TASK_STATUS_LABELS,
                           pop_charts=pop_charts)


# ── Patients ─────────────────────────────────────────────────────────────────

def _attach_modality_adherence(rows):
    """Attach cough_pct and clear_pct (30d) + modality flags + alert severity
    breakdown to each patient row for chip rendering."""
    augmented = []
    for r in rows:
        d = dict(r)
        mods = json.loads(r['rx_modalities']) if r['rx_modalities'] else []
        d['has_cough'] = 'cough' in mods
        d['has_clear'] = 'clear' in mods
        d['cough_pct'] = None
        d['clear_pct'] = None
        if d['has_cough']:
            _c, _g, pct = _modality_adherence(r['id'], 'cough')
            d['cough_pct'] = pct
        if d['has_clear']:
            _c, _g, pct = _modality_adherence(r['id'], 'clear')
            d['clear_pct'] = pct
        # Alert severity breakdown — used to color the chip
        sev = q_one("""SELECT
                       SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical,
                       SUM(CASE WHEN severity='warning'  THEN 1 ELSE 0 END) AS warning
                       FROM alerts WHERE patient_id = ? AND resolved_at IS NULL""",
                    (r['id'],))
        d['critical_alerts'] = sev['critical'] or 0
        d['warning_alerts']  = sev['warning'] or 0
        d['worst_severity'] = ('critical' if d['critical_alerts'] > 0
                               else ('warning' if d['warning_alerts'] > 0 else None))
        augmented.append(d)
    return augmented


# Sort key definitions. For modality sorts, `filter_fn` splits "has value"
# from "no value" so unprescribed patients always land at the bottom
# regardless of direction.
_PATIENT_SORT_CONFIG = {
    'name':         {'key': lambda p: ((p.get('last_name') or '').lower(),
                                       (p.get('first_name') or '').lower()),
                     'filter_fn': None},
    'location':     {'key': lambda p: (p.get('loc_name') or '').lower(),
                     'filter_fn': None},
    'disease':      {'key': lambda p: (p.get('diagnosis') or '').lower(),
                     'filter_fn': None},
    'cough_adh':    {'key': lambda p: p.get('cough_pct') or 0,
                     'filter_fn': lambda p: p.get('has_cough')},
    'clear_adh':    {'key': lambda p: p.get('clear_pct') or 0,
                     'filter_fn': lambda p: p.get('has_clear')},
    'alerts':       {'key': lambda p: (p.get('critical_alerts', 0) * 1000
                                       + p.get('warning_alerts', 0)),
                     'filter_fn': None},
    'last_session': {'key': lambda p: p.get('last_session_at') or '',
                     'filter_fn': None},
}


def _sort_patients(patients, sort_key, direction):
    cfg = _PATIENT_SORT_CONFIG.get(sort_key)
    if not cfg:
        return patients
    reverse = (direction == 'desc')
    if cfg['filter_fn']:
        has = [p for p in patients if cfg['filter_fn'](p)]
        has_not = [p for p in patients if not cfg['filter_fn'](p)]
        try:
            has_sorted = sorted(has, key=cfg['key'], reverse=reverse)
        except Exception:
            has_sorted = has
        return has_sorted + has_not
    try:
        return sorted(patients, key=cfg['key'], reverse=reverse)
    except Exception:
        return patients


@app.route('/api/patients/<int:patient_id>/chip/<modality>')
@require_login
def api_patient_chip(patient_id, modality):
    """Return modality-specific patient detail for the chip popover:
    adherence narrative, active alerts, and a chronological patient journey."""
    if modality not in ('cough', 'clear'):
        return jsonify(error='invalid modality'), 400
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p:
        return jsonify(error='not found'), 404

    modality_label = 'BiWaze Cough' if modality == 'cough' else 'BiWaze Clear'
    completed, goal, pct = _modality_adherence(patient_id, modality)

    # Short-window adherence for trend (last 7d vs prior 7-14d)
    last_7 = q_one("""SELECT COUNT(*) AS n FROM therapy_sessions
                      WHERE patient_id = ? AND session_type = ? AND completed = 1
                        AND started_at > datetime('now','-7 days')""",
                   (patient_id, modality))['n']
    prior_7 = q_one("""SELECT COUNT(*) AS n FROM therapy_sessions
                       WHERE patient_id = ? AND session_type = ? AND completed = 1
                         AND started_at <= datetime('now','-7 days')
                         AND started_at > datetime('now','-14 days')""",
                    (patient_id, modality))['n']
    if last_7 > prior_7:
        trend = 'improving'
    elif last_7 < prior_7:
        trend = 'declining'
    else:
        trend = 'stable'

    # Narrative
    name = f"{p['first_name']} {p['last_name']}"
    if pct >= 80:
        narrative = (f"{name} has strong {modality_label} adherence. "
                     f"{completed} of {goal} prescribed sessions completed in the last 30 days "
                     f"({pct}%). Recent 7-day trend: {trend}.")
    elif pct >= 50:
        narrative = (f"{name}'s {modality_label} adherence is below target. "
                     f"Only {completed} of {goal} prescribed sessions completed in the last 30 days "
                     f"({pct}%). Recent 7-day trend: {trend}. "
                     f"Consider a check-in call or device review.")
    else:
        narrative = (f"{name} has significant gaps in {modality_label} therapy. "
                     f"Only {completed} of {goal} prescribed sessions completed in the last 30 days "
                     f"({pct}%). Recent 7-day trend: {trend}. "
                     f"Immediate outreach recommended.")

    # Active alerts for this patient (patient-level, not modality-tagged)
    alerts = q_all("""SELECT a.*, r.name AS rule_name FROM alerts a
                      LEFT JOIN alert_rules r ON r.id = a.rule_id
                      WHERE a.patient_id = ? AND a.resolved_at IS NULL
                      ORDER BY CASE a.severity WHEN 'critical' THEN 1
                               WHEN 'warning' THEN 2 ELSE 3 END,
                               a.triggered_at DESC""", (patient_id,))
    alerts_list = [{
        'severity': a['severity'],
        'message': a['message'],
        'detail': a['detail'] or '',
        'rule_name': a['rule_name'] or '',
        'triggered_at': a['triggered_at'],
    } for a in alerts]

    journey = _build_patient_journey(patient_id, modality)

    return jsonify({
        'patient_name': name,
        'patient_id': patient_id,
        'modality': modality,
        'modality_label': modality_label,
        'adherence_pct': pct,
        'completed_30d': completed,
        'goal_30d': goal,
        'narrative': narrative,
        'trend': trend,
        'alerts': alerts_list,
        'journey': journey,
    })


@app.route('/patients/viz-compare')
@require_login
def patients_viz_compare():
    """Compare 4 visual concepts for combining adherence + alert count in one cell.
    Shows the same 5 representative patients in 4 sections, one per option."""
    oid = current_org_id()
    # Pick 5 representative patients spanning adherence + alert ranges
    sample_mrns = ['MRN-448239','MRN-44829','MRN-51022','MRN-50011','MRN-48990']
    placeholders = ','.join('?' * len(sample_mrns))
    rows = q_all(f"""SELECT p.*,
                     (SELECT COUNT(*) FROM alerts a WHERE a.patient_id = p.id
                        AND a.resolved_at IS NULL) AS open_alerts,
                     (SELECT COUNT(*) FROM alerts a WHERE a.patient_id = p.id
                        AND a.resolved_at IS NULL AND a.severity = 'critical') AS critical_alerts,
                     (SELECT COUNT(*) FROM alerts a WHERE a.patient_id = p.id
                        AND a.resolved_at IS NULL AND a.severity = 'warning') AS warning_alerts
                     FROM patients p
                     WHERE p.organization_id = ? AND p.mrn IN ({placeholders})
                     ORDER BY p.adherence_pct_30d ASC""",
                 (oid,) + tuple(sample_mrns))
    augmented = _attach_modality_adherence(rows)
    # worst_severity per patient for badge coloring
    for p in augmented:
        p['worst'] = ('critical' if p['critical_alerts'] > 0
                      else ('warning' if p['warning_alerts'] > 0 else None))
    return render_template('patients_viz_compare.html', patients=augmented)


@app.route('/patients')
@require_login
def patients():
    bounce = _parent_redirect_if_no_location('parent_patients')
    if bounce: return bounce
    oid = current_org_id()  # parent rollup handled via dedicated parent_patients route
    status = request.args.get('status', 'active')
    sort = request.args.get('sort', 'cough_adh')
    direction = request.args.get('dir', 'asc')
    rows = q_all(
        """SELECT p.*, u.first_name AS clinician_first, u.last_name AS clinician_last,
           (SELECT COUNT(*) FROM alerts a WHERE a.patient_id = p.id AND a.resolved_at IS NULL) AS open_alerts
           FROM patients p
           LEFT JOIN users u ON u.id = p.assigned_clinician_user_id
           WHERE p.organization_id = ? AND p.status = ?""",
        (oid, status))
    augmented = _attach_modality_adherence(rows)
    augmented = _sort_patients(augmented, sort, direction)
    status_counts = q_one("""SELECT
        SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
        SUM(CASE WHEN status='inactive' THEN 1 ELSE 0 END) AS inactive
        FROM patients WHERE organization_id = ?""", (oid,))
    return render_template('patients.html', patients=augmented, status=status,
                           sort=sort, direction=direction,
                           status_counts=status_counts)


THERAPY_GOAL_PER_DAY = 2


def _modality_adherence(patient_id, modality, days=30):
    """Return (completed_count, goal_count, pct) for a modality over last N days."""
    row = q_one("""SELECT COUNT(*) AS n FROM therapy_sessions
                   WHERE patient_id = ? AND session_type = ? AND completed = 1
                     AND started_at > datetime('now', ?)""",
                (patient_id, modality, f'-{days} days'))
    completed = row['n'] if row else 0
    goal = THERAPY_GOAL_PER_DAY * days
    capped = min(completed, goal)
    pct = round(100 * capped / goal) if goal else 0
    return completed, goal, pct


def _build_patient_journey(patient_id, modality):
    """Return chronologically-sorted list of patient journey events for a modality.
    Each event: {date, stage, label, detail, icon}. Shared by the chip popover API
    and the patient detail page."""
    modality_label = 'BiWaze Cough' if modality == 'cough' else 'BiWaze Clear'
    journey = []
    # 1. Prescription
    rx = q_one("""SELECT pp.prescribed_date, rp.first_name AS prov_first,
                  rp.last_name AS prov_last, rp.credentials AS prov_cred,
                  rc.name AS clinic_name
                  FROM patient_prescriptions pp
                  LEFT JOIN referring_providers rp ON rp.id = pp.prescribed_by_provider_id
                  LEFT JOIN referring_clinics rc ON rc.id = rp.clinic_id
                  WHERE pp.patient_id = ? AND pp.modality = ? AND pp.is_active = 1
                  ORDER BY pp.id DESC LIMIT 1""", (patient_id, modality))
    if rx and rx['prescribed_date']:
        prov = ''
        if rx['prov_last']:
            prov = f"Dr. {rx['prov_last']}"
            if rx['prov_cred']: prov += f", {rx['prov_cred']}"
            if rx['clinic_name']: prov += f" — {rx['clinic_name']}"
        journey.append({'date': rx['prescribed_date'], 'stage': 'prescription',
                        'label': 'Prescription issued',
                        'detail': prov or f'{modality_label} prescribed',
                        'icon': '📋'})

    # 2. Insurance approval — per device. Payers approve each BiWaze system
    # separately, so a dual-modality patient gets two journey events.
    ins = q_one("""SELECT payer_name, cough_approval_date, cough_auth_number,
                   clear_approval_date, clear_auth_number
                   FROM patient_insurance
                   WHERE patient_id = ? AND is_active = 1
                   ORDER BY id DESC LIMIT 1""", (patient_id,))
    if ins:
        approval_col = 'cough_approval_date' if modality == 'cough' else 'clear_approval_date'
        auth_col     = 'cough_auth_number'   if modality == 'cough' else 'clear_auth_number'
        if ins[approval_col]:
            auth = ins[auth_col]
            journey.append({'date': ins[approval_col], 'stage': 'insurance',
                            'label': f"{ins['payer_name']} approved {modality_label}",
                            'detail': f'Auth #{auth}' if auth else 'Authorization on file',
                            'icon': '✅'})

    # 3. Device assigned + training. (Shipping dates live upstream with the
    # manufacturer / distributor and aren't tracked by the portal, so we use
    # the assigned_date — when the HME handed the device to the patient.)
    model_filter = 'biwaze_cough' if modality == 'cough' else 'biwaze_clear'
    assign = q_one("""SELECT da.assigned_date, d.serial_number,
                      u.first_name AS by_first, u.last_name AS by_last
                      FROM device_assignments da
                      JOIN devices d ON d.id = da.device_id AND d.model = ?
                      LEFT JOIN users u ON u.id = da.assigned_by_user_id
                      WHERE da.patient_id = ? AND da.returned_date IS NULL
                      ORDER BY da.assigned_date DESC LIMIT 1""",
                   (model_filter, patient_id))
    if assign:
        journey.append({'date': assign['assigned_date'], 'stage': 'assigned',
                        'label': 'Device assigned',
                        'detail': f"Serial {assign['serial_number']}", 'icon': '📦'})
        trainer = (f"{assign['by_first']} {assign['by_last']}"
                   if assign['by_first'] else 'clinical team')
        journey.append({'date': assign['assigned_date'], 'stage': 'training',
                        'label': 'Home setup + training',
                        'detail': f"Trained by {trainer}", 'icon': '🏠'})

    # 4. First therapy session
    first = q_one("""SELECT MIN(started_at) AS started_at FROM therapy_sessions
                     WHERE patient_id = ? AND session_type = ? AND completed = 1""",
                  (patient_id, modality))
    if first and first['started_at']:
        journey.append({'date': first['started_at'][:10], 'stage': 'first_session',
                        'label': 'First therapy session',
                        'detail': modality_label, 'icon': '▶️'})

    # 5. Most recent session
    last = q_one("""SELECT MAX(started_at) AS started_at FROM therapy_sessions
                    WHERE patient_id = ? AND session_type = ? AND completed = 1""",
                 (patient_id, modality))
    if last and last['started_at']:
        if not first or last['started_at'][:10] != first['started_at'][:10]:
            journey.append({'date': last['started_at'][:10], 'stage': 'recent_session',
                            'label': 'Most recent session',
                            'detail': modality_label, 'icon': '⏱'})

    # 6. Survey milestones — only if engagement is enabled for this location.
    # Surveys are patient-level (not per-modality), so only emit them on the
    # first modality we render for a multi-modality patient to avoid duplicates.
    # We detect that by checking if this modality is the alphabetically-first
    # prescribed modality for the patient.
    p_org = q_one('SELECT organization_id FROM patients WHERE id = ?', (patient_id,))
    if p_org and feature_enabled('messaging_enabled', p_org['organization_id']):
        first_mod_row = q_one("""SELECT MIN(modality) AS m FROM patient_prescriptions
                                 WHERE patient_id = ? AND is_active = 1""",
                              (patient_id,))
        if first_mod_row and first_mod_row['m'] == modality:
            surveys = q_all("""SELECT milestone, score_0_100, completed_at,
                               opted_out, opted_out_at, opted_out_reason
                               FROM patient_surveys
                               WHERE patient_id = ? AND (completed_at IS NOT NULL OR opted_out = 1)
                               ORDER BY milestone""", (patient_id,))
            for s in surveys:
                if s['opted_out']:
                    journey.append({
                        'date': (s['opted_out_at'] or '')[:10],
                        'stage': 'survey_opt_out',
                        'label': f"{s['milestone']}-day survey declined",
                        'detail': s['opted_out_reason'] or 'Patient opted out',
                        'icon': '🙅',
                    })
                elif s['completed_at']:
                    score = s['score_0_100']
                    journey.append({
                        'date': s['completed_at'][:10],
                        'stage': 'survey',
                        'label': f"{s['milestone']}-day survey completed",
                        'detail': f"Score {score}%" if score is not None else 'Response captured',
                        'icon': '📝',
                    })

    # 7. Current gap warning OR no sessions ever
    from datetime import datetime as _dt2, date as _d2
    if last and last['started_at']:
        try:
            last_dt = _dt2.fromisoformat(last['started_at'].replace(' ', 'T'))
            hours_gap = (_dt2.now() - last_dt).total_seconds() / 3600
            if hours_gap > 48:
                journey.append({'date': _d2.today().isoformat(), 'stage': 'gap',
                                'label': 'Adherence gap',
                                'detail': f"No {modality} sessions in {int(hours_gap/24)} days",
                                'icon': '⚠️'})
        except Exception:
            pass
    else:
        journey.append({'date': _d2.today().isoformat(), 'stage': 'gap',
                        'label': 'No sessions yet',
                        'detail': f'Patient has not completed any {modality_label} sessions',
                        'icon': '⚠️'})

    journey.sort(key=lambda e: e.get('date') or '9999-99-99')
    return journey


def _daily_session_grid(patient_id, modality, days=30):
    """Return list of {date, count} for last N days (oldest first)."""
    rows = q_all("""SELECT DATE(started_at) AS d, COUNT(*) AS n
                    FROM therapy_sessions
                    WHERE patient_id = ? AND session_type = ? AND completed = 1
                      AND started_at > datetime('now', ?)
                    GROUP BY DATE(started_at)""",
                 (patient_id, modality, f'-{days} days'))
    by_date = {r['d']: r['n'] for r in rows}
    grid = []
    for i in range(days - 1, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        grid.append({'date': d, 'count': by_date.get(d, 0)})
    return grid


@app.route('/patients/<int:patient_id>')
@require_login
def patient_detail(patient_id):
    # Authorize via scope_org_ids so parent admins (rollup or acting in any
    # location) and super admins drilled into a customer org can both reach
    # individual patient detail.
    if not can_read_patient(patient_id):
        abort(404)
    p = q_one("""SELECT p.*, u.first_name AS clinician_first, u.last_name AS clinician_last,
                 rc.name AS clinic_name, rc.phone AS clinic_phone, rc.email AS clinic_email,
                 rc.website_url AS clinic_url, rc.npi AS clinic_npi,
                 rc.city AS clinic_city, rc.state AS clinic_state,
                 rp.first_name AS prov_first, rp.last_name AS prov_last,
                 rp.credentials AS prov_credentials, rp.specialty AS prov_specialty,
                 rp.phone AS prov_phone, rp.email AS prov_email, rp.npi AS prov_npi
                 FROM patients p
                 LEFT JOIN users u ON u.id = p.assigned_clinician_user_id
                 LEFT JOIN referring_clinics rc ON rc.id = p.referring_clinic_id
                 LEFT JOIN referring_providers rp ON rp.id = p.referring_provider_id
                 WHERE p.id = ?""", (patient_id,))
    if not p:
        abort(404)
    _log_access('patient_view', patient_id=patient_id, ref_type='patient', ref_id=patient_id)
    # Active device assignments
    assignments = q_all("""SELECT da.*, d.serial_number, d.model, d.firmware_version, d.status
                           FROM device_assignments da
                           JOIN devices d ON d.id = da.device_id
                           WHERE da.patient_id = ?
                           ORDER BY da.returned_date IS NULL DESC, da.assigned_date DESC""",
                        (patient_id,))
    alerts = q_all("""SELECT * FROM alerts WHERE patient_id = ?
                       ORDER BY resolved_at IS NULL DESC, triggered_at DESC LIMIT 20""",
                   (patient_id,))
    rx_modalities = json.loads(p['rx_modalities']) if p['rx_modalities'] else []

    # Per-modality adherence + daily grids + recent sessions + prescription
    therapy = {}
    for m in rx_modalities:
        completed, goal, pct = _modality_adherence(patient_id, m)
        recent = q_all("""SELECT id, started_at, duration_seconds, mode,
                          peak_pressure_cmh2o, peak_flow_lpm
                          FROM therapy_sessions
                          WHERE patient_id = ? AND session_type = ?
                          ORDER BY started_at DESC LIMIT 10""",
                       (patient_id, m))
        rx = q_one("""SELECT pp.*, rp.first_name AS prov_first, rp.last_name AS prov_last,
                      rp.credentials AS prov_cred
                      FROM patient_prescriptions pp
                      LEFT JOIN referring_providers rp ON rp.id = pp.prescribed_by_provider_id
                      WHERE pp.patient_id = ? AND pp.modality = ? AND pp.is_active = 1
                      ORDER BY pp.created_at DESC LIMIT 1""", (patient_id, m))
        therapy[m] = {
            'completed_30d': completed,
            'goal_30d': goal,
            'pct_30d': pct,
            'grid': _daily_session_grid(patient_id, m),
            'recent': recent,
            'rx': rx,
            'journey': _build_patient_journey(patient_id, m),
        }

    insurance = q_one("""SELECT * FROM patient_insurance
                         WHERE patient_id = ? AND is_active = 1
                         ORDER BY coverage_type LIMIT 1""", (patient_id,))

    # Most recent clinical observations (show top 3)
    clinical_recent = q_all("""SELECT * FROM patient_clinical_history
                               WHERE patient_id = ?
                               ORDER BY observation_date DESC LIMIT 3""", (patient_id,))
    clinical_total = q_one("SELECT COUNT(*) AS n FROM patient_clinical_history WHERE patient_id = ?",
                           (patient_id,))['n']

    # Last 30 days of mood check-ins (one dot per day — if multiple on same day, worst wins)
    mood_rows = q_all("""SELECT mood, response_text, recorded_at, date(recorded_at) AS d
                         FROM patient_moods WHERE patient_id = ?
                         AND date(recorded_at) >= date('now', '-29 days')
                         ORDER BY recorded_at""", (patient_id,))
    # Collapse multiple per day to worst mood
    _rank = {'sad': 0, 'meh': 1, 'ok': 2, 'happy': 3}
    mood_by_day = {}
    for r in mood_rows:
        existing = mood_by_day.get(r['d'])
        if not existing or _rank[r['mood']] < _rank[existing['mood']]:
            mood_by_day[r['d']] = dict(r)
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    mood_trend = []
    for i in range(30):
        day = today - _td(days=29 - i)
        key = day.isoformat()
        mood_trend.append({
            'date': key,
            'mood': mood_by_day[key]['mood'] if key in mood_by_day else None,
            'note': mood_by_day[key].get('response_text') if key in mood_by_day else None,
        })
    mood_recent_notes = q_all("""SELECT mood, response_text, recorded_at
                                 FROM patient_moods
                                 WHERE patient_id = ? AND response_text IS NOT NULL
                                   AND length(response_text) > 0
                                 ORDER BY recorded_at DESC LIMIT 5""", (patient_id,))

    # Surveys (all milestones completed so far)
    # Surveys: include both completed and opted-out so the milestone card can
    # render either state. A row with neither completed_at nor opted_out is a
    # survey that's been sent but not answered — we include those too so the
    # card differentiates "sent but no response" from "never sent."
    surveys = q_all("""SELECT * FROM patient_surveys WHERE patient_id = ?
                       ORDER BY milestone""", (patient_id,))

    # ── Patient goals (current week) ──
    # Each active goal + its 7-day running actuals for the week containing today.
    # Week starts on Monday so the calendar lines up with the mobile-app
    # experience patients see.
    from datetime import date as _d2, timedelta as _td2
    today2 = _d2.today()
    week_start = today2 - _td2(days=today2.weekday())
    week_end = week_start + _td2(days=6)
    active_goals = q_all("""SELECT * FROM patient_goals
                            WHERE patient_id = ? AND end_date IS NULL
                            ORDER BY
                              CASE goal_type
                                WHEN 'therapy_cough' THEN 1 WHEN 'therapy_clear' THEN 2
                                WHEN 'vest' THEN 3 WHEN 'breathing_treatment' THEN 4
                                WHEN 'steps' THEN 5 WHEN 'sleep' THEN 6 ELSE 9 END""",
                         (patient_id,))
    goals_week = []
    for g in active_goals:
        rows = q_all("""SELECT log_date, actual_value FROM patient_goal_log
                        WHERE goal_id = ? AND log_date BETWEEN date(?) AND date(?)
                        ORDER BY log_date""",
                     (g['id'], week_start.isoformat(), week_end.isoformat()))
        by_day = {r['log_date']: r['actual_value'] for r in rows}
        days = []
        for i in range(7):
            d = week_start + _td2(days=i)
            k = d.isoformat()
            actual = by_day.get(k)
            hit = (actual is not None and actual >= g['target_value'])
            days.append({
                'date': k,
                'weekday': d.strftime('%a'),
                'actual': actual,
                'hit': hit,
                'is_future': d > today2,
            })
        past_days = [d for d in days if not d['is_future']]
        met = sum(1 for d in past_days if d['hit'])
        goals_week.append({'goal': dict(g), 'days': days,
                           'met': met, 'eligible_days': len(past_days)})

    # Referral history (append-only audit of clinic/provider changes)
    referral_history = q_all("""SELECT rh.*,
                                c.name AS clinic_name,
                                rp.first_name AS prov_first, rp.last_name AS prov_last,
                                u.first_name AS by_first, u.last_name AS by_last
                                FROM patient_referral_history rh
                                LEFT JOIN referring_clinics c ON c.id = rh.clinic_id
                                LEFT JOIN referring_providers rp ON rp.id = rh.provider_id
                                LEFT JOIN users u ON u.id = rh.changed_by_user_id
                                WHERE rh.patient_id = ?
                                ORDER BY rh.assigned_at DESC""", (patient_id,))

    return render_template('patient_detail.html', patient=p, assignments=assignments,
                           alerts=alerts, rx_modalities=rx_modalities,
                           therapy=therapy, therapy_goal_per_day=THERAPY_GOAL_PER_DAY,
                           insurance=insurance,
                           clinical_recent=clinical_recent,
                           clinical_total=clinical_total,
                           mood_trend=mood_trend,
                           mood_recent_notes=mood_recent_notes,
                           surveys=surveys,
                           survey_questions=SURVEY_QUESTIONS,
                           goals_week=goals_week,
                           goals_week_start=week_start,
                           goals_week_end=week_end,
                           referral_history=referral_history)


# ══════════════════════════════════════════════════════════════════════
# Therapy Compliance Report (printable)
# ══════════════════════════════════════════════════════════════════════

def _compute_report_stats(patient_id, modality, start_date, end_date):
    """Aggregate the stats block shown at the top of each report page.
    Mirrors what's printed on the Summary.pdf: avg phases, sessions/day,
    totals, days with no therapy, avg + SD of primary pressures."""
    rows = q_all("""SELECT * FROM therapy_sessions
                    WHERE patient_id = ? AND session_type = ?
                      AND date(started_at) BETWEEN date(?) AND date(?)
                      AND completed = 1
                    ORDER BY started_at""",
                 (patient_id, modality, start_date.isoformat(), end_date.isoformat()))
    total_days = (end_date - start_date).days + 1
    days_with_therapy = len({s['started_at'][:10] for s in rows})
    days_no_therapy = max(0, total_days - days_with_therapy)
    total_sessions = len(rows)
    avg_per_day = (total_sessions / total_days) if total_days else 0

    pressures = [s['peak_pressure_cmh2o'] for s in rows if s['peak_pressure_cmh2o'] is not None]
    avg_pressure = (sum(pressures) / len(pressures)) if pressures else 0
    # Population SD
    if pressures:
        var = sum((p - avg_pressure) ** 2 for p in pressures) / len(pressures)
        sd_pressure = var ** 0.5
    else:
        sd_pressure = 0

    # Rough synthetic phase count: cough therapies typically cycle 4-5 phases
    # per session; clear sessions use PEP/OSC split. Use a sensible demo value.
    avg_phases = 4 if modality == 'cough' else 4

    return {
        'sessions': rows,
        'total_sessions': total_sessions,
        'total_days': total_days,
        'days_no_therapy': days_no_therapy,
        'days_with_therapy': days_with_therapy,
        'avg_per_day': avg_per_day,
        'avg_phases': avg_phases,
        'avg_pressure': avg_pressure,
        'sd_pressure': sd_pressure,
    }


@app.route('/patients/<int:patient_id>/report')
@require_login
def patient_report(patient_id):
    """Render the printable Therapy Compliance Report. Range defaults to 30d;
    overrideable via ?range=30|60|90 or ?start=YYYY-MM-DD&end=YYYY-MM-DD."""
    oid = current_org_id()
    p = q_one("""SELECT p.*, o.name AS org_name FROM patients p
                 LEFT JOIN organizations o ON o.id = p.organization_id
                 WHERE p.id = ? AND p.organization_id = ?""", (patient_id, oid))
    if not p: abort(404)

    from datetime import date as _date, timedelta as _td
    today = _date.today()
    range_key = request.args.get('range', '30')
    start_s = request.args.get('start')
    end_s   = request.args.get('end')
    if start_s and end_s:
        try:
            start_date = _date.fromisoformat(start_s)
            end_date   = _date.fromisoformat(end_s)
            range_key = 'custom'
        except ValueError:
            start_date = today - _td(days=30); end_date = today
            range_key = '30'
    else:
        days = {'30': 30, '60': 60, '90': 90}.get(range_key, 30)
        start_date = today - _td(days=days - 1)
        end_date = today
        range_key = str(days)

    # Modality: report generated per modality the patient is prescribed.
    # If ?modality=cough|clear limits to one; otherwise both (if applicable).
    filter_mod = request.args.get('modality')
    rx_rows = q_all("""SELECT DISTINCT modality FROM patient_prescriptions
                       WHERE patient_id = ? AND is_active = 1""", (patient_id,))
    rx_modalities = [r['modality'] for r in rx_rows]
    if filter_mod in ('cough', 'clear'):
        rx_modalities = [m for m in rx_modalities if m == filter_mod] or [filter_mod]
    if not rx_modalities:
        rx_modalities = ['cough']  # fallback so report always has something

    # Per-modality report data
    reports = []
    for modality in rx_modalities:
        stats = _compute_report_stats(patient_id, modality, start_date, end_date)
        # Active device serial + model for this modality
        device = q_one("""SELECT d.serial_number, d.model, d.firmware_version
                          FROM device_assignments da
                          JOIN devices d ON d.id = da.device_id
                          WHERE da.patient_id = ? AND da.returned_date IS NULL
                            AND d.model = ?
                          ORDER BY da.assigned_date DESC LIMIT 1""",
                       (patient_id, f'biwaze_{modality}'))
        reports.append({
            'modality': modality,
            'label': 'BiWaze Cough' if modality == 'cough' else 'BiWaze Clear',
            'device': device,
            'stats': stats,
        })

    _log_access('report_generate', patient_id=patient_id, ref_type='report',
                detail=f'{range_key}-day therapy summary ({start_date} to {end_date})')
    return render_template('patient_report.html',
                           patient=p, reports=reports,
                           start_date=start_date, end_date=end_date,
                           range_key=range_key,
                           generated_at=datetime.now())


@app.route('/patients/<int:patient_id>/insurance', methods=['GET', 'POST'])
@require_login
def patient_insurance_edit(patient_id):
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    ins = q_one("""SELECT * FROM patient_insurance
                   WHERE patient_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1""",
                (patient_id,))
    # Which BiWaze devices this patient has a live Rx for — drives which
    # approval-date fields the form renders.
    rx_modalities = [r['modality'] for r in q_all(
        """SELECT DISTINCT modality FROM patient_prescriptions
           WHERE patient_id = ? AND is_active = 1""", (patient_id,))]
    if request.method == 'POST':
        cough_approval = request.form.get('cough_approval_date') or None
        cough_auth     = request.form.get('cough_auth_number') or None
        clear_approval = request.form.get('clear_approval_date') or None
        clear_auth     = request.form.get('clear_auth_number') or None
        values = (request.form.get('payer_name'),
                  request.form.get('plan_type'),
                  request.form.get('member_id'),
                  request.form.get('group_number'),
                  request.form.get('coverage_type', 'primary'),
                  request.form.get('effective_date') or None,
                  cough_approval, cough_auth,
                  clear_approval, clear_auth,
                  request.form.get('next_review_date') or None,
                  request.form.get('notes'))
        if ins:
            q_exec("""UPDATE patient_insurance SET payer_name = ?, plan_type = ?, member_id = ?,
                      group_number = ?, coverage_type = ?, effective_date = ?,
                      cough_approval_date = ?, cough_auth_number = ?,
                      clear_approval_date = ?, clear_auth_number = ?,
                      next_review_date = ?, notes = ? WHERE id = ?""",
                   values + (ins['id'],))
        else:
            q_exec("""INSERT INTO patient_insurance (patient_id, payer_name, plan_type,
                      member_id, group_number, coverage_type, effective_date,
                      cough_approval_date, cough_auth_number,
                      clear_approval_date, clear_auth_number,
                      next_review_date, notes, is_active)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                   (patient_id,) + values)
        flash('Insurance updated.', 'success')
        return redirect(url_for('patient_detail', patient_id=patient_id))
    return render_template('patient_insurance_form.html', patient=p, insurance=ins,
                           rx_modalities=rx_modalities)


@app.route('/patients/<int:patient_id>/prescription/<modality>', methods=['GET', 'POST'])
@require_login
def patient_prescription_edit(patient_id, modality):
    if modality not in ('cough', 'clear'):
        abort(404)
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    rx = q_one("""SELECT * FROM patient_prescriptions
                  WHERE patient_id = ? AND modality = ? AND is_active = 1
                  ORDER BY id DESC LIMIT 1""", (patient_id, modality))
    # Referring providers at this org for the "prescribed by" dropdown
    providers = q_all("""SELECT rp.*, c.name AS clinic_name FROM referring_providers rp
                         JOIN referring_clinics c ON c.id = rp.clinic_id
                         WHERE rp.organization_id = ? AND rp.is_active = 1
                         ORDER BY c.name, rp.last_name""", (oid,))
    if request.method == 'POST':
        provider_id = request.form.get('prescribed_by_provider_id', type=int) or None

        # ── Handle uploaded prescription document ────────────────────
        # Precedence: if "remove_document" checked AND no new file → clear;
        # if new file uploaded → save it; otherwise carry the old path forward.
        doc_path = rx['document_path'] if rx else None
        doc_name = rx['document_filename'] if rx else None
        remove_doc = bool(request.form.get('remove_document'))
        rx_file = request.files.get('prescription_document')
        if rx_file and rx_file.filename:
            orig = rx_file.filename
            ext = orig.rsplit('.', 1)[-1].lower() if '.' in orig else ''
            if ext not in ALLOWED_RX_EXT:
                flash(f'Prescription upload must be one of: {", ".join(sorted(ALLOWED_RX_EXT))}.',
                      'error')
                return redirect(url_for('patient_prescription_edit',
                                        patient_id=patient_id, modality=modality))
            RX_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            safe = secure_filename(f'p{patient_id}_{modality}_{ts}.{ext}')
            rx_file.save(RX_DIR / safe)
            doc_path = f'uploads/prescriptions/{safe}'
            doc_name = secure_filename(orig)
        elif remove_doc:
            doc_path = None
            doc_name = None

        # Deactivate any existing active Rx for this modality (history preserved)
        q_exec("""UPDATE patient_prescriptions SET is_active = 0
                  WHERE patient_id = ? AND modality = ? AND is_active = 1""",
               (patient_id, modality))
        if modality == 'cough':
            q_exec("""INSERT INTO patient_prescriptions (patient_id, modality,
                      prescribed_by_provider_id, prescribed_date, effective_date,
                      cough_cycles, cough_insp_pressure_cmh2o, cough_insp_time_sec,
                      cough_exp_pressure_cmh2o, cough_exp_time_sec,
                      cough_pause_pressure_cmh2o, cough_pause_time_sec,
                      document_path, document_filename,
                      notes, is_active)
                      VALUES (?, 'cough', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                   (patient_id, provider_id,
                    request.form.get('prescribed_date') or None,
                    request.form.get('effective_date') or None,
                    request.form.get('cough_cycles', type=int),
                    request.form.get('cough_insp_pressure_cmh2o', type=float),
                    request.form.get('cough_insp_time_sec', type=float),
                    request.form.get('cough_exp_pressure_cmh2o', type=float),
                    request.form.get('cough_exp_time_sec', type=float),
                    request.form.get('cough_pause_pressure_cmh2o', type=float),
                    request.form.get('cough_pause_time_sec', type=float),
                    doc_path, doc_name,
                    request.form.get('notes')))
        else:
            neb = 1 if request.form.get('clear_neb_enabled') else 0
            bleed = 1 if request.form.get('clear_bleed_in_oxygen') else 0
            q_exec("""INSERT INTO patient_prescriptions (patient_id, modality,
                      prescribed_by_provider_id, prescribed_date, effective_date,
                      clear_pep_pressure_cmh2o, clear_pep_time_sec,
                      clear_osc_pressure_cmh2o, clear_osc_time_sec,
                      clear_neb_enabled, clear_neb_medication,
                      clear_bleed_in_oxygen, clear_oxygen_flow_lpm,
                      document_path, document_filename, notes, is_active)
                      VALUES (?, 'clear', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                   (patient_id, provider_id,
                    request.form.get('prescribed_date') or None,
                    request.form.get('effective_date') or None,
                    request.form.get('clear_pep_pressure_cmh2o', type=float),
                    # PEP/OSC time is entered as minutes + seconds in the form,
                    # stored as total seconds in the DB to match the legacy column.
                    ((request.form.get('clear_pep_time_min', type=int) or 0) * 60 +
                     (request.form.get('clear_pep_time_sec', type=int) or 0)),
                    request.form.get('clear_osc_pressure_cmh2o', type=float),
                    ((request.form.get('clear_osc_time_min', type=int) or 0) * 60 +
                     (request.form.get('clear_osc_time_sec', type=int) or 0)),
                    neb, request.form.get('clear_neb_medication') if neb else None,
                    bleed, request.form.get('clear_oxygen_flow_lpm', type=float) if bleed else None,
                    doc_path, doc_name,
                    request.form.get('notes')))
        flash(f'{modality.title()} prescription updated.', 'success')
        return redirect(url_for('patient_detail', patient_id=patient_id))
    return render_template('patient_prescription_form.html', patient=p, rx=rx,
                           modality=modality, providers=providers)


@app.route('/patients/<int:patient_id>/sessions')
@require_login
def patient_sessions(patient_id):
    """Full therapy session history for a patient. Filter by modality and
    date range; select rows to print."""
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    modality = request.args.get('modality', 'all')   # all|cough|clear
    start_s = request.args.get('start') or None
    end_s   = request.args.get('end') or None

    where = ['ts.patient_id = ?']
    params = [patient_id]
    if modality in ('cough', 'clear'):
        where.append('ts.session_type = ?'); params.append(modality)
    if start_s:
        where.append('date(ts.started_at) >= date(?)'); params.append(start_s)
    if end_s:
        where.append('date(ts.started_at) <= date(?)'); params.append(end_s)
    rows = q_all(f"""SELECT ts.*, d.serial_number, d.model AS device_model
                     FROM therapy_sessions ts
                     LEFT JOIN devices d ON d.id = ts.device_id
                     WHERE {' AND '.join(where)}
                     ORDER BY ts.started_at DESC""", tuple(params))
    # Availability flags drive which metric-picker groups to render. We only
    # show BiWaze Cough checkboxes if the patient actually has cough data in
    # the result set (likewise Clear), and only show SpO2 / HR if any of
    # their sessions carry those vitals.
    avail = q_one("""SELECT
        SUM(CASE WHEN session_type='cough' THEN 1 ELSE 0 END) AS cough_n,
        SUM(CASE WHEN session_type='clear' THEN 1 ELSE 0 END) AS clear_n,
        SUM(CASE WHEN spo2_pct IS NOT NULL THEN 1 ELSE 0 END) AS spo2_n,
        SUM(CASE WHEN heart_rate_bpm IS NOT NULL THEN 1 ELSE 0 END) AS hr_n
        FROM therapy_sessions WHERE patient_id = ?""", (patient_id,))
    has_cough = (avail['cough_n'] or 0) > 0
    has_clear = (avail['clear_n'] or 0) > 0
    has_spo2 = (avail['spo2_n'] or 0) > 0
    has_hr   = (avail['hr_n'] or 0) > 0
    return render_template('patient_sessions.html', patient=p, sessions=rows,
                           modality=modality, start=start_s, end=end_s,
                           has_cough=has_cough, has_clear=has_clear,
                           has_spo2=has_spo2, has_hr=has_hr)


@app.route('/patients/<int:patient_id>/sessions/print', methods=['GET', 'POST'])
@require_login
def patient_sessions_print(patient_id):
    """Printable selected-sessions report. IDs come from either the form
    submission (POST with ids[]) or the query string (?ids=1,2,3)."""
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    if request.method == 'POST':
        ids = request.form.getlist('ids', type=int)
        metrics = request.form.getlist('metrics')
    else:
        raw = request.args.get('ids', '')
        ids = [int(x) for x in raw.split(',') if x.isdigit()]
        metrics = request.args.getlist('metrics')
    if not ids:
        flash('Select at least one session to print.', 'error')
        return redirect(url_for('patient_sessions', patient_id=patient_id))
    allowed_metrics = {'peak_cough_flow', 'volume', 'peak_insp_pressure',
                       'peak_pressure', 'spo2', 'heart_rate'}
    metrics = [m for m in metrics if m in allowed_metrics]
    placeholders = ','.join('?' * len(ids))
    rows = q_all(f"""SELECT ts.*, d.serial_number, d.model AS device_model
                     FROM therapy_sessions ts
                     LEFT JOIN devices d ON d.id = ts.device_id
                     WHERE ts.patient_id = ? AND ts.id IN ({placeholders})
                     ORDER BY ts.started_at""",
                 (patient_id,) + tuple(ids))
    # Decode the waveform JSON for each session so the print template can
    # render the pressure-over-time graph inline (same data session_detail
    # uses). Bad JSON is non-fatal — card just omits the graph.
    sessions = []
    for r in rows:
        d = dict(r)
        wf = None
        if r['waveform_json']:
            try:
                wf = json.loads(r['waveform_json'])
            except Exception:
                wf = None
        d['waveform'] = wf
        sessions.append(d)
    _log_access('report_generate', patient_id=patient_id, ref_type='report',
                detail=f'Selected-sessions print ({len(sessions)} sessions)')
    return render_template('patient_sessions_print.html', patient=p,
                           sessions=sessions, metrics=metrics,
                           generated_at=datetime.now())


@app.route('/patients/<int:patient_id>/sessions/<int:session_id>')
@require_login
def session_detail(patient_id, session_id):
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    s = q_one("""SELECT ts.*, d.serial_number, d.model AS device_model
                 FROM therapy_sessions ts
                 LEFT JOIN devices d ON d.id = ts.device_id
                 WHERE ts.id = ? AND ts.patient_id = ?""",
              (session_id, patient_id))
    if not s: abort(404)
    _log_access('session_view', patient_id=patient_id, ref_type='session', ref_id=session_id)
    wf = json.loads(s['waveform_json']) if s['waveform_json'] else None
    return render_template('session_detail.html', patient=p, session=s, waveform=wf)


@app.route('/patients/<int:patient_id>/goals')
@require_login
def patient_goals(patient_id):
    """Goals history for a patient — filterable by date range. Default
    range is the last 30 days. Renders one row per goal × day with the
    target, actual, and hit/miss status."""
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    from datetime import date as _d3, timedelta as _td3
    today3 = _d3.today()
    start_s = request.args.get('start')
    end_s   = request.args.get('end')
    goal_filter = request.args.get('goal_type') or ''
    try:
        end_date = _d3.fromisoformat(end_s) if end_s else today3
    except ValueError:
        end_date = today3
    try:
        start_date = _d3.fromisoformat(start_s) if start_s else (end_date - _td3(days=29))
    except ValueError:
        start_date = end_date - _td3(days=29)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    goals = q_all("""SELECT * FROM patient_goals WHERE patient_id = ?
                     ORDER BY end_date IS NULL DESC, goal_type""", (patient_id,))
    if goal_filter:
        goals = [g for g in goals if g['goal_type'] == goal_filter]
    # For each goal, build a day-by-day table within [start, end] ∩ goal's active range.
    goal_rows = []
    for g in goals:
        gs = _d3.fromisoformat(g['start_date'])
        ge = _d3.fromisoformat(g['end_date']) if g['end_date'] else today3
        span_start = max(start_date, gs)
        span_end = min(end_date, ge)
        if span_start > span_end:
            continue
        logs = q_all("""SELECT log_date, actual_value FROM patient_goal_log
                        WHERE goal_id = ? AND log_date BETWEEN date(?) AND date(?)
                        ORDER BY log_date""",
                     (g['id'], span_start.isoformat(), span_end.isoformat()))
        by_day = {r['log_date']: r['actual_value'] for r in logs}
        days = []
        met = 0
        total_actual = 0.0
        n_logged = 0
        cur = span_start
        while cur <= span_end:
            k = cur.isoformat()
            actual = by_day.get(k)
            hit = (actual is not None and actual >= g['target_value'])
            if hit: met += 1
            if actual is not None:
                total_actual += actual
                n_logged += 1
            days.append({'date': k, 'weekday': cur.strftime('%a'),
                         'actual': actual, 'hit': hit})
            cur += _td3(days=1)
        goal_rows.append({
            'goal': dict(g),
            'days': days,
            'met': met,
            'total_days': len(days),
            'hit_pct': round(100 * met / len(days)) if days else 0,
            'avg_actual': round(total_actual / n_logged, 1) if n_logged else None,
        })
    return render_template('patient_goals.html', patient=p,
                           goal_rows=goal_rows,
                           start_date=start_date, end_date=end_date,
                           goal_filter=goal_filter,
                           all_goal_types=[g['goal_type'] for g in q_all(
                               "SELECT DISTINCT goal_type FROM patient_goals WHERE patient_id = ?",
                               (patient_id,))])


@app.route('/patients/<int:patient_id>/edit', methods=['GET', 'POST'])
@require_login
def patient_edit(patient_id):
    oid = current_org_id()
    patient = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
                    (patient_id, oid))
    if not patient: abort(404)
    if request.method == 'POST':
        mods = request.form.getlist('rx_modalities')
        clinician_id = request.form.get('assigned_clinician_user_id', type=int) or None
        clinic_id = request.form.get('referring_clinic_id', type=int) or None
        provider_id = request.form.get('referring_provider_id', type=int) or None
        if provider_id:
            prov = q_one('SELECT clinic_id FROM referring_providers WHERE id = ? AND organization_id = ?',
                         (provider_id, oid))
            if prov: clinic_id = prov['clinic_id']
        q_exec("""UPDATE patients SET mrn = ?, first_name = ?, last_name = ?, dob = ?,
                  phone = ?, email = ?, address_line1 = ?, address_line2 = ?,
                  city = ?, state = ?, zip = ?,
                  preferred_language = ?, rx_frequency_per_day = ?, rx_modalities = ?,
                  assigned_clinician_user_id = ?, referring_clinic_id = ?,
                  referring_provider_id = ?, diagnosis = ?
                  WHERE id = ? AND organization_id = ?""",
               (request.form.get('mrn'), request.form.get('first_name'),
                request.form.get('last_name'), request.form.get('dob') or None,
                request.form.get('phone'), request.form.get('email'),
                request.form.get('address_line1'), request.form.get('address_line2'),
                request.form.get('city'), request.form.get('state'), request.form.get('zip'),
                request.form.get('preferred_language', 'en-US'),
                request.form.get('rx_frequency_per_day', type=int),
                json.dumps(mods), clinician_id, clinic_id, provider_id,
                request.form.get('diagnosis'),
                patient_id, oid))
        flash('Patient updated.', 'success')
        return redirect(url_for('patient_detail', patient_id=patient_id))
    clinicians = q_all("""SELECT * FROM users WHERE organization_id = ? AND is_active = 1
                          AND role IN ('clinician', 'admin') ORDER BY last_name""", (oid,))
    clinics = q_all("""SELECT * FROM referring_clinics WHERE organization_id = ? AND is_active = 1
                       ORDER BY name""", (oid,))
    providers = q_all("""SELECT rp.*, c.name AS clinic_name FROM referring_providers rp
                         JOIN referring_clinics c ON c.id = rp.clinic_id
                         WHERE rp.organization_id = ? AND rp.is_active = 1
                         ORDER BY c.name, rp.last_name""", (oid,))
    p_mods = json.loads(patient['rx_modalities']) if patient['rx_modalities'] else []
    return render_template('patient_form.html', clinicians=clinicians,
                           clinics=clinics, providers=providers,
                           patient=patient, patient_mods=p_mods)


# Clinical history
@app.route('/patients/<int:patient_id>/clinical-history')
@require_login
def patient_clinical_history(patient_id):
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    rows = q_all("""SELECT h.*, u.first_name AS by_first, u.last_name AS by_last
                    FROM patient_clinical_history h
                    LEFT JOIN users u ON u.id = h.recorded_by_user_id
                    WHERE h.patient_id = ?
                    ORDER BY h.observation_date DESC""", (patient_id,))
    return render_template('patient_clinical_history.html', patient=p, history=rows)


@app.route('/patients/<int:patient_id>/clinical-history/new', methods=['GET', 'POST'])
@require_login
def patient_clinical_history_new(patient_id):
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    if request.method == 'POST':
        on_o2 = 1 if request.form.get('on_oxygen_therapy') else 0
        q_exec("""INSERT INTO patient_clinical_history (patient_id, observation_date,
                  hospital_admission_date, hospital_discharge_date, admission_reason,
                  spo2_pct, fev1_liters, fev1_pct_predicted, fvc_liters, fvc_pct_predicted,
                  on_oxygen_therapy, oxygen_flow_lpm, oxygen_type, notes, recorded_by_user_id)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (patient_id, request.form.get('observation_date') or date.today().isoformat(),
                request.form.get('hospital_admission_date') or None,
                request.form.get('hospital_discharge_date') or None,
                request.form.get('admission_reason'),
                request.form.get('spo2_pct', type=int),
                request.form.get('fev1_liters', type=float),
                request.form.get('fev1_pct_predicted', type=int),
                request.form.get('fvc_liters', type=float),
                request.form.get('fvc_pct_predicted', type=int),
                on_o2,
                request.form.get('oxygen_flow_lpm', type=float) if on_o2 else None,
                request.form.get('oxygen_type') if on_o2 else None,
                request.form.get('notes'),
                current_user()['id']))
        flash('Clinical observation added.', 'success')
        return redirect(url_for('patient_clinical_history', patient_id=patient_id))
    return render_template('patient_clinical_history_form.html', patient=p, obs=None,
                           today=date.today().isoformat())


@app.route('/patients/<int:patient_id>/clinical-history/<int:obs_id>', methods=['GET', 'POST'])
@require_login
def patient_clinical_history_edit(patient_id, obs_id):
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    obs = q_one('SELECT * FROM patient_clinical_history WHERE id = ? AND patient_id = ?',
                (obs_id, patient_id))
    if not obs: abort(404)
    if request.method == 'POST':
        on_o2 = 1 if request.form.get('on_oxygen_therapy') else 0
        q_exec("""UPDATE patient_clinical_history SET observation_date = ?,
                  hospital_admission_date = ?, hospital_discharge_date = ?, admission_reason = ?,
                  spo2_pct = ?, fev1_liters = ?, fev1_pct_predicted = ?,
                  fvc_liters = ?, fvc_pct_predicted = ?,
                  on_oxygen_therapy = ?, oxygen_flow_lpm = ?, oxygen_type = ?, notes = ?
                  WHERE id = ?""",
               (request.form.get('observation_date') or date.today().isoformat(),
                request.form.get('hospital_admission_date') or None,
                request.form.get('hospital_discharge_date') or None,
                request.form.get('admission_reason'),
                request.form.get('spo2_pct', type=int),
                request.form.get('fev1_liters', type=float),
                request.form.get('fev1_pct_predicted', type=int),
                request.form.get('fvc_liters', type=float),
                request.form.get('fvc_pct_predicted', type=int),
                on_o2,
                request.form.get('oxygen_flow_lpm', type=float) if on_o2 else None,
                request.form.get('oxygen_type') if on_o2 else None,
                request.form.get('notes'),
                obs_id))
        flash('Observation updated.', 'success')
        return redirect(url_for('patient_clinical_history', patient_id=patient_id))
    return render_template('patient_clinical_history_form.html', patient=p, obs=obs)


@app.route('/patients/<int:patient_id>/clinical-history/<int:obs_id>/delete', methods=['POST'])
@require_login
def patient_clinical_history_delete(patient_id, obs_id):
    oid = current_org_id()
    p = q_one('SELECT id FROM patients WHERE id = ? AND organization_id = ?',
              (patient_id, oid))
    if not p: abort(404)
    q_exec('DELETE FROM patient_clinical_history WHERE id = ? AND patient_id = ?',
           (obs_id, patient_id))
    flash('Observation deleted.', 'success')
    return redirect(url_for('patient_clinical_history', patient_id=patient_id))


@app.route('/patients/new', methods=['GET', 'POST'])
@require_login
def patient_new():
    oid = current_org_id()
    if request.method == 'POST':
        mods = request.form.getlist('rx_modalities')
        clinician_id = request.form.get('assigned_clinician_user_id', type=int) or None
        clinic_id = request.form.get('referring_clinic_id', type=int) or None
        provider_id = request.form.get('referring_provider_id', type=int) or None
        # If provider is set, derive the clinic from the provider for consistency
        if provider_id:
            prov = q_one('SELECT clinic_id FROM referring_providers WHERE id = ? AND organization_id = ?',
                         (provider_id, oid))
            if prov:
                clinic_id = prov['clinic_id']
        q_exec("""INSERT INTO patients (organization_id, mrn, first_name, last_name, dob,
                  phone, email, address_line1, address_line2, city, state, zip,
                  preferred_language, rx_frequency_per_day, rx_modalities,
                  assigned_clinician_user_id, referring_clinic_id, referring_provider_id,
                  diagnosis, status)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
               (oid, request.form.get('mrn'), request.form.get('first_name'),
                request.form.get('last_name'), request.form.get('dob') or None,
                request.form.get('phone'), request.form.get('email'),
                request.form.get('address_line1'), request.form.get('address_line2'),
                request.form.get('city'), request.form.get('state'), request.form.get('zip'),
                request.form.get('preferred_language', 'en-US'),
                request.form.get('rx_frequency_per_day', type=int),
                json.dumps(mods), clinician_id, clinic_id, provider_id,
                request.form.get('diagnosis')))
        flash('Patient added.', 'success')
        return redirect(url_for('patients'))
    clinicians = q_all("""SELECT * FROM users WHERE organization_id = ? AND is_active = 1
                          AND role IN ('clinician', 'admin') ORDER BY last_name""", (oid,))
    clinics = q_all("""SELECT * FROM referring_clinics WHERE organization_id = ? AND is_active = 1
                       ORDER BY name""", (oid,))
    providers = q_all("""SELECT rp.*, c.name AS clinic_name FROM referring_providers rp
                         JOIN referring_clinics c ON c.id = rp.clinic_id
                         WHERE rp.organization_id = ? AND rp.is_active = 1
                         ORDER BY c.name, rp.last_name""", (oid,))
    return render_template('patient_form.html', clinicians=clinicians,
                           clinics=clinics, providers=providers)


# ── Devices ──────────────────────────────────────────────────────────────────

@app.route('/devices')
@require_login
def devices():
    bounce = _parent_redirect_if_no_location('parent_devices')
    if bounce: return bounce
    oid = current_org_id()
    tab = request.args.get('tab', 'all')
    if tab == 'assigned':
        rows = q_all("""SELECT d.*, p.id AS patient_id, p.first_name, p.last_name, p.mrn,
                        da.assigned_date, da.consent_form_path
                        FROM devices d
                        JOIN device_assignments da ON da.device_id = d.id AND da.returned_date IS NULL
                        JOIN patients p ON p.id = da.patient_id
                        WHERE d.organization_id = ?
                        ORDER BY d.serial_number""", (oid,))
    elif tab == 'unassigned':
        rows = q_all("""SELECT d.*, NULL AS patient_id, NULL AS first_name, NULL AS last_name,
                        NULL AS mrn, NULL AS assigned_date, NULL AS consent_form_path
                        FROM devices d
                        WHERE d.organization_id = ?
                          AND NOT EXISTS (SELECT 1 FROM device_assignments da
                                          WHERE da.device_id = d.id AND da.returned_date IS NULL)
                        ORDER BY d.serial_number""", (oid,))
    else:
        rows = q_all("""SELECT d.*, p.id AS patient_id, p.first_name, p.last_name, p.mrn,
                        da.assigned_date, da.consent_form_path
                        FROM devices d
                        LEFT JOIN device_assignments da ON da.device_id = d.id AND da.returned_date IS NULL
                        LEFT JOIN patients p ON p.id = da.patient_id
                        WHERE d.organization_id = ?
                        ORDER BY d.serial_number""", (oid,))
    # KPIs across all devices
    all_devices = q_all('SELECT status FROM devices WHERE organization_id = ?', (oid,))
    kpis = {
        'total': len(all_devices),
        'in_use': len([d for d in all_devices if d['status'] == 'in_use']),
        'in_stock': len([d for d in all_devices if d['status'] == 'in_stock']),
        'maintenance': len([d for d in all_devices if d['status'] == 'maintenance']),
    }
    return render_template('devices.html', devices=rows, tab=tab, kpis=kpis)


@app.route('/devices/new', methods=['GET', 'POST'])
@require_login
def device_new():
    oid = current_org_id()
    if request.method == 'POST':
        serial = request.form.get('serial_number', '').strip()
        model = request.form.get('model')
        existing = q_one('SELECT id FROM devices WHERE serial_number = ?', (serial,))
        if existing:
            flash(f'Device with serial {serial} already exists.', 'error')
            return redirect(url_for('device_new'))
        q_exec("""INSERT INTO devices (organization_id, serial_number, model, firmware_version,
                  upload_date, warranty_end, status, notes)
                  VALUES (?, ?, ?, ?, ?, ?, 'in_stock', ?)""",
               (oid, serial, model, request.form.get('firmware_version'),
                request.form.get('upload_date') or date.today().isoformat(),
                request.form.get('warranty_end') or None,
                request.form.get('notes')))
        flash(f'Device {serial} added to inventory.', 'success')
        return redirect(url_for('devices', tab='unassigned'))
    return render_template('device_form.html', device=None)


@app.route('/devices/<int:device_id>')
@require_login
def device_detail(device_id):
    oid = current_org_id()
    d = q_one('SELECT * FROM devices WHERE id = ? AND organization_id = ?',
              (device_id, oid))
    if not d:
        abort(404)
    assignments = q_all("""SELECT da.*, p.first_name, p.last_name, p.mrn, p.id AS patient_id,
                           u.first_name AS by_first, u.last_name AS by_last
                           FROM device_assignments da
                           JOIN patients p ON p.id = da.patient_id
                           LEFT JOIN users u ON u.id = da.assigned_by_user_id
                           WHERE da.device_id = ? ORDER BY da.assigned_date DESC""",
                        (device_id,))
    return render_template('device_detail.html', device=d, assignments=assignments)


@app.route('/devices/<int:device_id>/assign', methods=['GET', 'POST'])
@require_login
def device_assign(device_id):
    oid = current_org_id()
    d = q_one('SELECT * FROM devices WHERE id = ? AND organization_id = ?',
              (device_id, oid))
    if not d:
        abort(404)
    # Current assignment (if any) — presence means this is a REASSIGNMENT
    active = q_one("""SELECT da.*, p.first_name, p.last_name, p.mrn, p.id AS patient_id
                      FROM device_assignments da
                      JOIN patients p ON p.id = da.patient_id
                      WHERE da.device_id = ? AND da.returned_date IS NULL""",
                   (device_id,))

    if request.method == 'POST':
        patient_id = request.form.get('patient_id', type=int)
        if not patient_id:
            flash('Select a patient.', 'error')
            return redirect(url_for('device_assign', device_id=device_id))

        # Verify patient is in this org
        p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?',
                  (patient_id, oid))
        if not p:
            abort(404)

        # Consent form file is REQUIRED
        consent_file = request.files.get('consent_form')
        if not consent_file or not consent_file.filename:
            flash('A signed consent form (PDF) is required to assign a device.',
                  'error')
            return redirect(url_for('device_assign', device_id=device_id))

        ext = consent_file.filename.rsplit('.', 1)[-1].lower() if '.' in consent_file.filename else ''
        if ext not in ALLOWED_CONSENT_EXT:
            flash(f'Consent form must be one of: {", ".join(sorted(ALLOWED_CONSENT_EXT))}.',
                  'error')
            return redirect(url_for('device_assign', device_id=device_id))

        CONSENT_DIR.mkdir(parents=True, exist_ok=True)
        safe_orig = secure_filename(consent_file.filename)
        uniq = uuid.uuid4().hex[:8]
        filename = f'{d["serial_number"]}_{p["mrn"] or p["id"]}_{uniq}.{ext}'
        filename = secure_filename(filename)
        consent_file.save(CONSENT_DIR / filename)

        assigned_date = request.form.get('assigned_date') or date.today().isoformat()
        notes = request.form.get('notes', '')

        # If this is a reassignment, close the active assignment first
        if active:
            if active['patient_id'] == patient_id:
                flash('Device is already assigned to that patient.', 'error')
                return redirect(url_for('device_assign', device_id=device_id))
            q_exec("UPDATE device_assignments SET returned_date = ? WHERE id = ?",
                   (assigned_date, active['id']))

        q_exec("""INSERT INTO device_assignments (patient_id, device_id, assigned_date,
                  consent_form_path, consent_form_original_name, assigned_by_user_id, notes)
                  VALUES (?, ?, ?, ?, ?, ?, ?)""",
               (patient_id, device_id, assigned_date,
                f'uploads/consent/{filename}', safe_orig,
                current_user()['id'], notes))
        q_exec("UPDATE devices SET status = 'in_use' WHERE id = ?", (device_id,))

        if active:
            flash(f'Device {d["serial_number"]} reassigned from {active["first_name"]} {active["last_name"]} to {p["first_name"]} {p["last_name"]}.',
                  'success')
        else:
            flash(f'Device {d["serial_number"]} assigned to {p["first_name"]} {p["last_name"]}.',
                  'success')
        return redirect(url_for('patient_detail', patient_id=patient_id))

    patients_list = q_all("""SELECT * FROM patients WHERE organization_id = ? AND status = 'active'
                             ORDER BY last_name, first_name""", (oid,))
    return render_template('device_assign.html', device=d, patients=patients_list,
                           today=date.today().isoformat(),
                           current_assignment=active)


@app.route('/devices/<int:device_id>/unassign/<int:assignment_id>', methods=['POST'])
@require_login
def device_unassign(device_id, assignment_id):
    oid = current_org_id()
    d = q_one('SELECT * FROM devices WHERE id = ? AND organization_id = ?',
              (device_id, oid))
    if not d:
        abort(404)
    q_exec("UPDATE device_assignments SET returned_date = DATE('now') WHERE id = ? AND device_id = ?",
           (assignment_id, device_id))
    q_exec("UPDATE devices SET status = 'in_stock' WHERE id = ?", (device_id,))
    flash(f'Device {d["serial_number"]} returned to inventory.', 'success')
    return redirect(url_for('device_detail', device_id=device_id))


# ── Alerts ───────────────────────────────────────────────────────────────────

@app.route('/alerts')
@require_login
def alerts():
    bounce = _parent_redirect_if_no_location('parent_alerts')
    if bounce: return bounce
    oid = current_org_id()
    u = current_user()
    filter_sev = request.args.get('severity', 'all')
    show = request.args.get('show', 'active')

    # Each alert gets the id + assignee of its linked open task (if any),
    # so the alert card can swap "Assign" for "Task #N — Jane".
    query = """SELECT a.*, p.first_name, p.last_name, p.mrn, p.id AS patient_id,
               r.name AS rule_name,
               (SELECT t.id FROM tasks t WHERE t.alert_id = a.id
                AND t.status NOT IN ('completed','cancelled') LIMIT 1) AS task_id,
               (SELECT t.assigned_to_user_id FROM tasks t WHERE t.alert_id = a.id
                AND t.status NOT IN ('completed','cancelled') LIMIT 1) AS task_assignee_id,
               (SELECT tu.first_name FROM tasks t
                JOIN users tu ON tu.id = t.assigned_to_user_id
                WHERE t.alert_id = a.id AND t.status NOT IN ('completed','cancelled') LIMIT 1) AS task_assignee_first
               FROM alerts a
               JOIN patients p ON p.id = a.patient_id
               LEFT JOIN alert_rules r ON r.id = a.rule_id
               WHERE a.organization_id = ?"""
    params = [oid]
    if show == 'active':
        query += ' AND a.resolved_at IS NULL'
    elif show == 'mine':
        # Alerts whose open linked task is assigned to current user
        query += """ AND a.resolved_at IS NULL AND EXISTS (
                     SELECT 1 FROM tasks t2 WHERE t2.alert_id = a.id
                     AND t2.assigned_to_user_id = ?
                     AND t2.status NOT IN ('completed','cancelled'))"""
        params.append(u['id'])
    if filter_sev in ('info', 'warning', 'critical'):
        query += ' AND a.severity = ?'
        params.append(filter_sev)
    query += """ ORDER BY CASE a.severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                a.triggered_at DESC"""
    rows = q_all(query, tuple(params))

    # Counts for tabs
    counts = q_one(
        """SELECT
           SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS active,
           SUM(CASE WHEN resolved_at IS NULL AND severity='critical' THEN 1 ELSE 0 END) AS critical,
           SUM(CASE WHEN resolved_at IS NULL AND severity='warning' THEN 1 ELSE 0 END) AS warning,
           SUM(CASE WHEN acknowledged_at IS NOT NULL AND resolved_at IS NULL THEN 1 ELSE 0 END) AS acknowledged
           FROM alerts WHERE organization_id = ?""", (oid,))
    mine_count = q_one("""SELECT COUNT(*) AS n FROM alerts a WHERE a.organization_id = ?
                          AND a.resolved_at IS NULL AND EXISTS (
                            SELECT 1 FROM tasks t WHERE t.alert_id = a.id
                            AND t.assigned_to_user_id = ?
                            AND t.status NOT IN ('completed','cancelled'))""",
                       (oid, u['id']))
    counts = dict(counts) if counts else {}
    counts['mine'] = mine_count['n'] if mine_count else 0

    assign_users = q_all("""SELECT id, first_name, last_name FROM users
                            WHERE organization_id = ? AND is_active = 1
                            ORDER BY last_name""", (oid,))
    # Pre-compute default datetime-local values per severity for the inline picker
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()
    default_due = {
        sev: (now + _td(hours=hrs)).strftime('%Y-%m-%dT%H:%M')
        for sev, hrs in TASK_DUE_DEFAULTS.items()
    }
    return render_template('alerts.html', alerts=rows, filter_sev=filter_sev,
                           show=show, counts=counts, assign_users=assign_users,
                           default_due=default_due, due_defaults=TASK_DUE_DEFAULTS)


@app.route('/alerts/bulk', methods=['POST'])
@require_login
def alert_bulk():
    """Bulk-apply an action (acknowledge / resolve) to multiple alerts.
    Form payload: ids=[...]&action=acknowledge|resolve"""
    oid = current_org_id()
    u = current_user()
    ids = request.form.getlist('ids', type=int)
    action = request.form.get('action')
    if not ids:
        flash('Select at least one alert first.', 'error')
        return redirect(request.referrer or url_for('alerts'))
    placeholders = ','.join('?' * len(ids))
    params = tuple(ids) + (oid,)
    if action == 'acknowledge':
        q_exec(f"""UPDATE alerts SET acknowledged_by_user_id = ?,
                   acknowledged_at = CURRENT_TIMESTAMP
                   WHERE id IN ({placeholders}) AND organization_id = ?
                   AND acknowledged_at IS NULL""",
               (u['id'],) + params)
        flash(f'{len(ids)} alert{"s" if len(ids) != 1 else ""} acknowledged.', 'success')
    elif action == 'resolve':
        q_exec(f"""UPDATE alerts SET resolved_at = CURRENT_TIMESTAMP
                   WHERE id IN ({placeholders}) AND organization_id = ?
                   AND resolved_at IS NULL""", params)
        # Cascade-close any linked open tasks for the alerts we just resolved
        q_exec(f"""UPDATE tasks SET status = 'completed',
                   completed_at = CURRENT_TIMESTAMP, completed_by_user_id = ?
                   WHERE alert_id IN ({placeholders}) AND organization_id = ?
                   AND status NOT IN ('completed','cancelled')""",
               (u['id'],) + params)
        flash(f'{len(ids)} alert{"s" if len(ids) != 1 else ""} resolved.', 'success')
    else:
        abort(400)
    return redirect(request.referrer or url_for('alerts'))


@app.route('/alerts/<int:alert_id>/acknowledge', methods=['POST'])
@require_login
def alert_acknowledge(alert_id):
    oid = current_org_id()
    q_exec("""UPDATE alerts SET acknowledged_by_user_id = ?, acknowledged_at = CURRENT_TIMESTAMP
              WHERE id = ? AND organization_id = ? AND acknowledged_at IS NULL""",
           (current_user()['id'], alert_id, oid))
    flash('Alert acknowledged.', 'success')
    return redirect(request.referrer or url_for('alerts'))


@app.route('/alerts/<int:alert_id>/resolve', methods=['POST'])
@require_login
def alert_resolve(alert_id):
    oid = current_org_id()
    u = current_user()
    q_exec("""UPDATE alerts SET resolved_at = CURRENT_TIMESTAMP
              WHERE id = ? AND organization_id = ? AND resolved_at IS NULL""",
           (alert_id, oid))
    # Cascade: auto-complete any open tasks linked to this alert
    linked = q_all("""SELECT id FROM tasks WHERE alert_id = ? AND organization_id = ?
                      AND status NOT IN ('completed','cancelled')""", (alert_id, oid))
    for t_row in linked:
        q_exec("""UPDATE tasks SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
                  completed_by_user_id = ? WHERE id = ?""",
               (u['id'], t_row['id']))
        q_exec("""INSERT INTO task_activity (task_id, user_id, kind, detail)
                  VALUES (?, ?, 'status_changed', 'Auto-completed: alert resolved')""",
               (t_row['id'], u['id']))
    flash(f'Alert resolved.' + (f' {len(linked)} linked task{"s" if len(linked)!=1 else ""} auto-completed.' if linked else ''),
          'success')
    return redirect(request.referrer or url_for('alerts'))


# ── Settings: location info ──────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@require_login
@require_admin
def settings():
    oid = current_org_id()
    if request.method == 'POST':
        default_assignee = request.form.get('default_assignee_user_id', type=int) or None
        # Validate default assignee is a user at this location
        if default_assignee:
            assignee = q_one('SELECT id FROM users WHERE id = ? AND organization_id = ?',
                             (default_assignee, oid))
            if not assignee: default_assignee = None
        q_exec("""UPDATE organizations SET name = ?, phone = ?, email = ?,
                  address_line1 = ?, address_line2 = ?, city = ?, state = ?, zip = ?,
                  timezone = ?, default_assignee_user_id = ? WHERE id = ?""",
               (request.form.get('name'), request.form.get('phone'),
                request.form.get('email'), request.form.get('address_line1'),
                request.form.get('address_line2'), request.form.get('city'),
                request.form.get('state'), request.form.get('zip'),
                request.form.get('timezone', 'America/New_York'),
                default_assignee, oid))
        # Logo upload (optional)
        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            ext = logo_file.filename.rsplit('.', 1)[-1].lower() if '.' in logo_file.filename else ''
            if ext in ALLOWED_LOGO_EXT:
                LOGOS_DIR.mkdir(parents=True, exist_ok=True)
                fn = secure_filename(f'{oid}.{ext}')
                logo_file.save(LOGOS_DIR / fn)
                q_exec('UPDATE organizations SET logo_path = ? WHERE id = ?',
                       (f'uploads/logos/{fn}', oid))
            else:
                flash(f'Logo must be one of: {", ".join(sorted(ALLOWED_LOGO_EXT))}.',
                      'error')
        flash('Location settings updated.', 'success')
        return redirect(url_for('settings'))
    org = current_org()
    parent_override_on = False
    if org and org['parent_id']:
        parent = q_one("""SELECT messaging_enabled, mood_response_enabled
                          FROM organizations WHERE id = ?""", (org['parent_id'],))
        # Whole feature group considered overridden if parent disabled messaging.
        parent_override_on = bool(parent and not parent['messaging_enabled'])
    users_list = q_all("""SELECT id, first_name, last_name, role FROM users
                          WHERE organization_id = ? AND is_active = 1
                          ORDER BY last_name, first_name""", (oid,))
    # Heat-map layer selection: applied at the parent-org level for group
    # admins (so the network rollup view is consistent), or the satellite
    # for satellite admins.
    hm_settings_oid = _heatmap_settings_org_id() or oid
    hm_enabled_keys = _enabled_heatmap_layers(hm_settings_oid)
    return render_template('settings.html', org=org, users=users_list,
                           parent_override_on=parent_override_on,
                           heatmap_layers=HEATMAP_LAYERS,
                           heatmap_enabled_keys=set(hm_enabled_keys))


@app.route('/settings/alert-policy', methods=['POST'])
@require_login
@require_admin
def settings_alert_policy():
    """Parent-admin only: set who manages alert rules across child locations."""
    u = current_user()
    org = current_org()
    if not org or org['type'] != 'parent' or u['role'] != 'admin':
        flash('Only the parent group admin can change this setting.', 'error')
        return redirect(url_for('settings'))
    source = request.form.get('alert_rules_source', 'location')
    if source not in ('location', 'parent'):
        source = 'location'
    q_exec('UPDATE organizations SET alert_rules_source = ? WHERE id = ?',
           (source, org['id']))
    flash('Alert-rule policy updated.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/engagement', methods=['POST'])
@require_login
@require_admin
def settings_engagement():
    """Save the patient-engagement feature toggles for this location."""
    oid = current_org_id()
    org = q_one('SELECT parent_id FROM organizations WHERE id = ?', (oid,))
    # If parent has messaging off, don't allow the child to write an enabled value.
    parent_blocks = False
    if org and org['parent_id']:
        parent = q_one('SELECT messaging_enabled FROM organizations WHERE id = ?',
                       (org['parent_id'],))
        parent_blocks = bool(parent and not parent['messaging_enabled'])
    if parent_blocks:
        flash('Parent organization has disabled patient engagement — cannot change here.',
              'error')
        return redirect(url_for('settings'))
    messaging = 1 if request.form.get('messaging_enabled') else 0
    mood_resp = 1 if request.form.get('mood_response_enabled') else 0
    q_exec("""UPDATE organizations
              SET messaging_enabled = ?, mood_response_enabled = ?
              WHERE id = ?""", (messaging, mood_resp, oid))
    flash('Patient engagement settings updated.', 'success')
    return redirect(url_for('settings'))


# ── Settings: audit log ─────────────────────────────────────────────────────

AUDIT_EVENT_LABELS = {
    'patient_view':    'Patient viewed',
    'session_view':    'Therapy session viewed',
    'report_generate': 'Report generated',
    'data_export':     'Data exported',
    'task_activity':   'Task activity',
    'location_create': 'Location created',
    'location_update': 'Location updated',
    'user_create':     'User created',
    'user_update':     'User updated',
}


@app.route('/settings/audit-log')
@require_login
@require_admin
def audit_log():
    """Admin-only audit log. Parent admins see all child locations; location
    admins see only their own location's events."""
    u = current_user()
    filter_event = request.args.get('event', 'all')
    filter_user  = request.args.get('user', type=int) or 0
    filter_patient = request.args.get('patient', type=int) or 0
    filter_location = request.args.get('location', type=int) or 0
    show_external = request.args.get('source') == 'abmrc'   # ABMRC accesses tab

    # Scope: parent admins see every child location + themselves; location
    # admins only their own org.
    if is_parent_admin():
        child_ids = _child_location_ids(u)
        scope_ids = list(child_ids) + [u['organization_id']]
    else:
        scope_ids = [current_org_id()]
    # Parent admins can narrow by a specific child location; reject ids
    # outside their scope so query strings can't leak across orgs.
    if filter_location and filter_location in scope_ids:
        query_scope = [filter_location]
    else:
        filter_location = 0
        query_scope = scope_ids

    query = f"""SELECT al.*, usr.first_name AS user_first, usr.last_name AS user_last,
               p.first_name AS pt_first, p.last_name AS pt_last, p.mrn AS pt_mrn,
               o.name AS org_name
               FROM access_log al
               LEFT JOIN users usr ON usr.id = al.user_id
               LEFT JOIN patients p ON p.id = al.patient_id
               LEFT JOIN organizations o ON o.id = al.organization_id
               WHERE al.organization_id IN ({','.join('?' * len(query_scope))})"""
    params = list(query_scope)
    if filter_event != 'all':
        query += ' AND al.event = ?'; params.append(filter_event)
    if filter_user:
        query += ' AND al.user_id = ?'; params.append(filter_user)
    if filter_patient:
        query += ' AND al.patient_id = ?'; params.append(filter_patient)
    if show_external:
        query += ' AND al.is_external_access = 1'
    query += ' ORDER BY al.occurred_at DESC LIMIT 500'
    events = q_all(query, tuple(params))

    # Counts for the two top-level tabs (internal vs ABMRC access).
    counts_query = f"""SELECT
        SUM(CASE WHEN is_external_access = 0 THEN 1 ELSE 0 END) AS internal,
        SUM(CASE WHEN is_external_access = 1 THEN 1 ELSE 0 END) AS external
        FROM access_log
        WHERE organization_id IN ({','.join('?' * len(query_scope))})"""
    audit_counts = q_one(counts_query, tuple(query_scope))

    # Users dropdown: scoped to the selected location (or all child locations
    # for a parent admin with no location filter) so the picker only shows
    # relevant actors.
    user_scope = [filter_location] if filter_location else scope_ids
    users = q_all(f"""SELECT id, first_name, last_name FROM users
                     WHERE organization_id IN ({','.join('?' * len(user_scope))})
                     ORDER BY last_name""", tuple(user_scope))

    # Location list for the filter dropdown — only meaningful for parent admins.
    locations = []
    if is_parent_admin():
        locations = q_all(
            f"""SELECT id, name FROM organizations
                WHERE id IN ({','.join('?' * len(scope_ids))})
                ORDER BY (type = 'parent') DESC, name""", tuple(scope_ids))

    return render_template('audit_log.html', events=events, users=users,
                           filter_event=filter_event, filter_user=filter_user,
                           filter_patient=filter_patient,
                           filter_location=filter_location,
                           locations=locations,
                           event_labels=AUDIT_EVENT_LABELS,
                           is_parent=is_parent_admin(),
                           show_external=show_external,
                           audit_counts=audit_counts)


# ── Mobile app feature-gating endpoint ──────────────────────────────────────

@app.route('/api/patient/<int:patient_id>/features')
@require_login
def api_patient_features(patient_id):
    """Mobile-app poll: returns the engagement features currently available
    for this patient's owning location. The Arc Connect mobile app hides the
    message composer and mood-response field based on these flags so patient
    messages never go to an unmonitored inbox."""
    p = q_one('SELECT organization_id FROM patients WHERE id = ?', (patient_id,))
    if not p: abort(404)
    return jsonify({
        'messaging_enabled':     feature_enabled('messaging_enabled', p['organization_id']),
        'mood_capture_enabled':  True,  # always on — mood is always captured
        'mood_response_enabled': feature_enabled('mood_response_enabled', p['organization_id']),
    })


# ── Settings: users ──────────────────────────────────────────────────────────

@app.route('/settings/users')
@require_login
@require_admin
def users():
    oid = current_org_id()
    rows = q_all("""SELECT * FROM users WHERE organization_id = ?
                    ORDER BY is_active DESC, role, last_name""", (oid,))
    return render_template('users.html', users=rows)


@app.route('/settings/users/new', methods=['GET', 'POST'])
@require_login
@require_admin
def user_new():
    # Parent admins may pre-target a specific child location via ?location=<id>.
    # Otherwise the new user is scoped to the admin's current acting org.
    target_org_id = current_org_id()
    target_location = None
    requested_loc = request.values.get('location', type=int)
    if requested_loc and is_parent_admin():
        u = current_user()
        target_location = q_one(
            """SELECT * FROM organizations
               WHERE id = ? AND parent_id = ? AND type = 'location'""",
            (requested_loc, u['organization_id']))
        if target_location:
            target_org_id = target_location['id']
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        existing = q_one('SELECT id FROM users WHERE email = ?', (email,))
        if existing:
            flash('A user with that email already exists.', 'error')
            return redirect(url_for('user_new', location=requested_loc or None))
        q_exec("""INSERT INTO users (organization_id, email, first_name, last_name, role, phone, is_active)
                  VALUES (?, ?, ?, ?, ?, ?, 1)""",
               (target_org_id, email, request.form.get('first_name'),
                request.form.get('last_name'), request.form.get('role'),
                request.form.get('phone')))
        new_uid = q_one('SELECT last_insert_rowid() AS id')['id']
        _log_access('user_create', ref_type='user', ref_id=new_uid,
                    detail=f'Created {email} at org {target_org_id}')
        flash(f'User {email} added.', 'success')
        if target_location:
            return redirect(url_for('parent_overview'))
        return redirect(url_for('users'))
    return render_template('user_form.html', user=None,
                           target_location=target_location)


@app.route('/settings/users/<int:user_id>/edit', methods=['GET', 'POST'])
@require_login
@require_admin
def user_edit(user_id):
    oid = current_org_id()
    u = q_one('SELECT * FROM users WHERE id = ? AND organization_id = ?',
              (user_id, oid))
    if not u: abort(404)
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        # Email must remain unique across the system (excluding this user's
        # own row, since they may be saving without changing their email).
        conflict = q_one('SELECT id FROM users WHERE email = ? AND id != ?',
                         (email, user_id))
        if conflict:
            flash('Another user already has that email.', 'error')
            return redirect(url_for('user_edit', user_id=user_id))
        role = request.form.get('role')
        if role not in ('admin', 'clinician', 'billing', 'read_only',
                        'customer_service', 'account_executive'):
            flash('Invalid role.', 'error')
            return redirect(url_for('user_edit', user_id=user_id))
        # Don't let an admin strip the last active admin role from the org —
        # otherwise the location loses its admin.
        if u['role'] == 'admin' and role != 'admin':
            other_admins = q_one("""SELECT COUNT(*) AS n FROM users
                                    WHERE organization_id = ? AND role = 'admin'
                                      AND is_active = 1 AND id != ?""",
                                 (oid, user_id))['n']
            if other_admins == 0:
                flash('Cannot change role: this is the only active admin for the location.',
                      'error')
                return redirect(url_for('user_edit', user_id=user_id))
        q_exec("""UPDATE users SET first_name = ?, last_name = ?, email = ?,
                  role = ?, phone = ? WHERE id = ?""",
               (request.form.get('first_name'),
                request.form.get('last_name'),
                email, role,
                request.form.get('phone') or None,
                user_id))
        _log_access('user_update', ref_type='user', ref_id=user_id,
                    detail=f'Updated {email} (role={role})')
        flash(f'User {email} updated.', 'success')
        return redirect(url_for('users'))
    return render_template('user_form.html', user=u)


@app.route('/settings/users/<int:user_id>/deactivate', methods=['POST'])
@require_login
@require_admin
def user_deactivate(user_id):
    oid = current_org_id()
    u = q_one('SELECT * FROM users WHERE id = ? AND organization_id = ?',
              (user_id, oid))
    if not u:
        abort(404)
    if u['id'] == current_user()['id']:
        flash('You cannot deactivate your own account.', 'error')
    else:
        q_exec('UPDATE users SET is_active = 0 WHERE id = ?', (user_id,))
        flash(f'User {u["email"]} deactivated.', 'success')
    return redirect(url_for('users'))


@app.route('/settings/users/<int:user_id>/activate', methods=['POST'])
@require_login
@require_admin
def user_activate(user_id):
    oid = current_org_id()
    q_exec('UPDATE users SET is_active = 1 WHERE id = ? AND organization_id = ?',
           (user_id, oid))
    flash('User reactivated.', 'success')
    return redirect(url_for('users'))


# ── Settings: alert rules ────────────────────────────────────────────────────

METRICS = [
    ('missed_therapy_days', 'Missed Therapy Days',
     'days', 'no therapy for N days'),
    ('device_disconnected_hours', 'Hours since last device communication',
     'hours', 'device offline for N hours'),
    ('adherence_pct_drop', 'Drop in 30-day adherence vs prior 30 days',
     'percentage points', 'adherence dropped N%'),
]


@app.route('/settings/alert-rules')
@require_login
@require_admin
def alert_rules():
    oid, parent_managed, can_edit = alert_rules_context()
    parent_org = None
    if parent_managed:
        parent_org = q_one('SELECT id, name FROM organizations WHERE id = ?', (oid,))
    rows = q_all('SELECT * FROM alert_rules WHERE organization_id = ? ORDER BY severity, name',
                 (oid,))
    return render_template('alert_rules.html', rules=rows, metrics=METRICS,
                           parent_managed=parent_managed, can_edit=can_edit,
                           managing_parent=parent_org)


@app.route('/settings/alert-rules/new', methods=['GET', 'POST'])
@require_login
@require_admin
def alert_rule_new():
    oid, parent_managed, can_edit = alert_rules_context()
    if not can_edit:
        flash('Alert rules for this location are managed by the parent organization.',
              'info')
        return redirect(url_for('alert_rules'))
    if request.method == 'POST':
        roles = request.form.getlist('notify_recipient_roles')
        q_exec("""INSERT INTO alert_rules (organization_id, name, description, metric,
                  threshold_value, window_hours, severity, notify_email, notify_in_app,
                  notify_sms, notify_recipient_roles, is_active)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (oid, request.form.get('name'), request.form.get('description'),
                request.form.get('metric'),
                request.form.get('threshold_value', type=float),
                request.form.get('window_hours', type=int),
                request.form.get('severity'),
                1 if request.form.get('notify_email') else 0,
                1 if request.form.get('notify_in_app') else 0,
                1 if request.form.get('notify_sms') else 0,
                json.dumps(roles),
                1 if request.form.get('is_active') else 0))
        flash('Alert rule created.', 'success')
        return redirect(url_for('alert_rules'))
    return render_template('alert_rule_form.html', rule=None, metrics=METRICS)


@app.route('/settings/alert-rules/<int:rule_id>', methods=['GET', 'POST'])
@require_login
@require_admin
def alert_rule_edit(rule_id):
    oid, parent_managed, can_edit = alert_rules_context()
    if not can_edit:
        flash('Alert rules for this location are managed by the parent organization.',
              'info')
        return redirect(url_for('alert_rules'))
    r = q_one('SELECT * FROM alert_rules WHERE id = ? AND organization_id = ?',
              (rule_id, oid))
    if not r:
        abort(404)
    if request.method == 'POST':
        roles = request.form.getlist('notify_recipient_roles')
        q_exec("""UPDATE alert_rules SET name = ?, description = ?, metric = ?,
                  threshold_value = ?, window_hours = ?, severity = ?, notify_email = ?,
                  notify_in_app = ?, notify_sms = ?, notify_recipient_roles = ?,
                  is_active = ? WHERE id = ?""",
               (request.form.get('name'), request.form.get('description'),
                request.form.get('metric'),
                request.form.get('threshold_value', type=float),
                request.form.get('window_hours', type=int),
                request.form.get('severity'),
                1 if request.form.get('notify_email') else 0,
                1 if request.form.get('notify_in_app') else 0,
                1 if request.form.get('notify_sms') else 0,
                json.dumps(roles),
                1 if request.form.get('is_active') else 0, rule_id))
        flash('Alert rule updated.', 'success')
        return redirect(url_for('alert_rules'))
    rule_roles = json.loads(r['notify_recipient_roles']) if r['notify_recipient_roles'] else []
    return render_template('alert_rule_form.html', rule=r, rule_roles=rule_roles,
                           metrics=METRICS)


@app.route('/settings/alert-rules/<int:rule_id>/delete', methods=['POST'])
@require_login
@require_admin
def alert_rule_delete(rule_id):
    oid, parent_managed, can_edit = alert_rules_context()
    if not can_edit:
        flash('Alert rules for this location are managed by the parent organization.',
              'info')
        return redirect(url_for('alert_rules'))
    q_exec('DELETE FROM alert_rules WHERE id = ? AND organization_id = ?',
           (rule_id, oid))
    flash('Alert rule deleted.', 'success')
    return redirect(url_for('alert_rules'))


# ── Settings: referring clinics ──────────────────────────────────────────────

@app.route('/settings/referring-clinics')
@require_login
def referring_clinics():
    scope_ids = scope_org_ids()
    rollup = is_rollup_scope()
    if not scope_ids:
        return render_template('referring_clinics.html', clinics=[], rollup=rollup,
                               status_filter='all', filter_location=0, locations=[])
    status_filter = request.args.get('status', 'all')
    if status_filter not in ('all', 'active', 'inactive'):
        status_filter = 'all'
    filter_location = request.args.get('location', type=int) or 0
    if rollup and filter_location and filter_location in scope_ids:
        query_ids = [filter_location]
    else:
        filter_location = 0 if not rollup else filter_location
        query_ids = scope_ids
    if not rollup:
        filter_location = 0
    ph = ','.join('?' * len(query_ids))
    where = [f'c.organization_id IN ({ph})']
    params = list(query_ids)
    if status_filter == 'active':
        where.append('c.is_active = 1')
    elif status_filter == 'inactive':
        where.append('c.is_active = 0')
    rows = q_all(f"""SELECT c.*, o.id AS loc_id, o.name AS loc_name,
                    (SELECT COUNT(*) FROM referring_providers rp
                       WHERE rp.clinic_id = c.id AND rp.is_active = 1) AS provider_count,
                    (SELECT COUNT(*) FROM patients p
                       WHERE p.referring_clinic_id = c.id) AS patient_count
                    FROM referring_clinics c
                    JOIN organizations o ON o.id = c.organization_id
                    WHERE {' AND '.join(where)}""",
                 tuple(params))
    locations = []
    if rollup:
        loc_ph = ','.join('?' * len(scope_ids))
        locations = q_all(
            f"SELECT id, name FROM organizations WHERE id IN ({loc_ph}) ORDER BY name",
            tuple(scope_ids))
    return render_template('referring_clinics.html', clinics=rows, rollup=rollup,
                           status_filter=status_filter,
                           filter_location=filter_location, locations=locations)


@app.route('/settings/referring-clinics/<int:clinic_id>/view')
@require_login
def referring_clinic_detail(clinic_id):
    """Drill-down: clinic info + its providers list."""
    oid = current_org_id()
    clinic = q_one("""SELECT c.*,
                      (SELECT COUNT(*) FROM patients p WHERE p.referring_clinic_id = c.id) AS patient_count
                      FROM referring_clinics c
                      WHERE c.id = ? AND c.organization_id = ?""", (clinic_id, oid))
    if not clinic: abort(404)
    providers = q_all("""SELECT rp.*,
                         (SELECT COUNT(*) FROM patients p
                            WHERE p.referring_provider_id = rp.id) AS patient_count
                         FROM referring_providers rp
                         WHERE rp.clinic_id = ? AND rp.organization_id = ?
                         ORDER BY rp.is_active DESC, rp.last_name, rp.first_name""",
                      (clinic_id, oid))
    # Full referral history for this clinic — who was referred, when, and
    # whether they're still linked.
    referral_history = q_all("""SELECT rh.*, p.first_name, p.last_name, p.mrn,
                                rp.first_name AS prov_first, rp.last_name AS prov_last
                                FROM patient_referral_history rh
                                JOIN patients p ON p.id = rh.patient_id
                                LEFT JOIN referring_providers rp ON rp.id = rh.provider_id
                                WHERE rh.clinic_id = ?
                                ORDER BY (rh.removed_at IS NULL) DESC, rh.assigned_at DESC""",
                             (clinic_id,))
    return render_template('referring_clinic_detail.html',
                           clinic=clinic, providers=providers,
                           referral_history=referral_history)


@app.route('/settings/referring-clinics/new', methods=['GET', 'POST'])
@require_login
def referring_clinic_new():
    oid = current_org_id()
    if request.method == 'POST':
        q_exec("""INSERT INTO referring_clinics (organization_id, name, npi, phone, email,
                  website_url, address_line1, address_line2, city, state, zip, notes, is_active)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
               (oid, request.form.get('name'), request.form.get('npi'),
                request.form.get('phone'), request.form.get('email'),
                request.form.get('website_url'),
                request.form.get('address_line1'), request.form.get('address_line2'),
                request.form.get('city'), request.form.get('state'),
                request.form.get('zip'), request.form.get('notes')))
        flash('Referring clinic added.', 'success')
        return redirect(url_for('referring_clinics'))
    return render_template('referring_clinic_form.html', clinic=None)


@app.route('/settings/referring-clinics/<int:clinic_id>', methods=['GET', 'POST'])
@require_login
def referring_clinic_edit(clinic_id):
    oid = current_org_id()
    clinic = q_one('SELECT * FROM referring_clinics WHERE id = ? AND organization_id = ?',
                   (clinic_id, oid))
    if not clinic: abort(404)
    if request.method == 'POST':
        q_exec("""UPDATE referring_clinics SET name = ?, npi = ?, phone = ?, email = ?,
                  website_url = ?, address_line1 = ?, address_line2 = ?,
                  city = ?, state = ?, zip = ?, notes = ? WHERE id = ?""",
               (request.form.get('name'), request.form.get('npi'),
                request.form.get('phone'), request.form.get('email'),
                request.form.get('website_url'),
                request.form.get('address_line1'), request.form.get('address_line2'),
                request.form.get('city'), request.form.get('state'),
                request.form.get('zip'), request.form.get('notes'), clinic_id))
        flash('Referring clinic updated.', 'success')
        return redirect(url_for('referring_clinic_detail', clinic_id=clinic_id))
    return render_template('referring_clinic_form.html', clinic=clinic)


@app.route('/settings/referring-clinics/<int:clinic_id>/deactivate', methods=['POST'])
@require_login
def referring_clinic_deactivate(clinic_id):
    oid = current_org_id()
    q_exec("UPDATE referring_clinics SET is_active = 0 WHERE id = ? AND organization_id = ?",
           (clinic_id, oid))
    flash('Clinic deactivated.', 'success')
    return redirect(request.referrer or url_for('referring_clinics'))


@app.route('/settings/referring-clinics/<int:clinic_id>/activate', methods=['POST'])
@require_login
def referring_clinic_activate(clinic_id):
    oid = current_org_id()
    q_exec("UPDATE referring_clinics SET is_active = 1 WHERE id = ? AND organization_id = ?",
           (clinic_id, oid))
    flash('Clinic reactivated.', 'success')
    return redirect(request.referrer or url_for('referring_clinics'))


@app.route('/settings/referring-clinics/<int:clinic_id>/delete', methods=['POST'])
@require_login
@require_admin
def referring_clinic_delete(clinic_id):
    """Hard delete — admin only. Cascades to providers, NULLs patient refs."""
    oid = current_org_id()
    clinic = q_one('SELECT name FROM referring_clinics WHERE id = ? AND organization_id = ?',
                   (clinic_id, oid))
    if not clinic: abort(404)
    q_exec('DELETE FROM referring_clinics WHERE id = ? AND organization_id = ?',
           (clinic_id, oid))
    flash(f'Referring clinic "{clinic["name"]}" deleted.', 'success')
    return redirect(url_for('referring_clinics'))


# ── Settings: referring providers ────────────────────────────────────────────

@app.route('/settings/referring-providers')
@require_login
def referring_providers():
    oid = current_org_id()
    rows = q_all("""SELECT rp.*, c.name AS clinic_name,
                    (SELECT COUNT(*) FROM patients p
                       WHERE p.referring_provider_id = rp.id) AS patient_count
                    FROM referring_providers rp
                    JOIN referring_clinics c ON c.id = rp.clinic_id
                    WHERE rp.organization_id = ?
                    ORDER BY c.name, rp.is_active DESC, rp.last_name""", (oid,))
    return render_template('referring_providers.html', providers=rows)


@app.route('/settings/referring-providers/new', methods=['GET', 'POST'])
@require_login
def referring_provider_new():
    oid = current_org_id()
    clinics = q_all("""SELECT * FROM referring_clinics WHERE organization_id = ? AND is_active = 1
                       ORDER BY name""", (oid,))
    if not clinics:
        flash('Add at least one referring clinic first.', 'error')
        return redirect(url_for('referring_clinic_new'))
    prefill_clinic_id = request.args.get('clinic_id', type=int)
    if request.method == 'POST':
        clinic_id = request.form.get('clinic_id', type=int)
        # Verify clinic is in this org
        clinic = q_one('SELECT id FROM referring_clinics WHERE id = ? AND organization_id = ?',
                       (clinic_id, oid))
        if not clinic: abort(404)
        q_exec("""INSERT INTO referring_providers (organization_id, clinic_id, first_name,
                  last_name, credentials, specialty, npi, phone, email, notes, is_active)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
               (oid, clinic_id, request.form.get('first_name'),
                request.form.get('last_name'), request.form.get('credentials'),
                request.form.get('specialty'), request.form.get('npi'),
                request.form.get('phone'), request.form.get('email'),
                request.form.get('notes')))
        flash('Referring provider added.', 'success')
        return redirect(url_for('referring_clinic_detail', clinic_id=clinic_id))
    return render_template('referring_provider_form.html', provider=None, clinics=clinics,
                           prefill_clinic_id=prefill_clinic_id)


@app.route('/settings/referring-providers/<int:provider_id>', methods=['GET', 'POST'])
@require_login
def referring_provider_edit(provider_id):
    oid = current_org_id()
    provider = q_one('SELECT * FROM referring_providers WHERE id = ? AND organization_id = ?',
                     (provider_id, oid))
    if not provider: abort(404)
    clinics = q_all("""SELECT * FROM referring_clinics WHERE organization_id = ? AND is_active = 1
                       ORDER BY name""", (oid,))
    if request.method == 'POST':
        clinic_id = request.form.get('clinic_id', type=int)
        clinic = q_one('SELECT id FROM referring_clinics WHERE id = ? AND organization_id = ?',
                       (clinic_id, oid))
        if not clinic: abort(404)
        q_exec("""UPDATE referring_providers SET clinic_id = ?, first_name = ?, last_name = ?,
                  credentials = ?, specialty = ?, npi = ?, phone = ?, email = ?, notes = ?
                  WHERE id = ?""",
               (clinic_id, request.form.get('first_name'),
                request.form.get('last_name'), request.form.get('credentials'),
                request.form.get('specialty'), request.form.get('npi'),
                request.form.get('phone'), request.form.get('email'),
                request.form.get('notes'), provider_id))
        flash('Referring provider updated.', 'success')
        return redirect(url_for('referring_clinic_detail', clinic_id=clinic_id))
    return render_template('referring_provider_form.html', provider=provider, clinics=clinics)


@app.route('/settings/referring-providers/<int:provider_id>/deactivate', methods=['POST'])
@require_login
def referring_provider_deactivate(provider_id):
    oid = current_org_id()
    q_exec("UPDATE referring_providers SET is_active = 0 WHERE id = ? AND organization_id = ?",
           (provider_id, oid))
    flash('Provider deactivated.', 'success')
    return redirect(request.referrer or url_for('referring_providers'))


@app.route('/settings/referring-providers/<int:provider_id>/activate', methods=['POST'])
@require_login
def referring_provider_activate(provider_id):
    oid = current_org_id()
    q_exec("UPDATE referring_providers SET is_active = 1 WHERE id = ? AND organization_id = ?",
           (provider_id, oid))
    flash('Provider reactivated.', 'success')
    return redirect(request.referrer or url_for('referring_providers'))


@app.route('/settings/referring-providers/<int:provider_id>/delete', methods=['POST'])
@require_login
@require_admin
def referring_provider_delete(provider_id):
    """Hard delete — admin only. Sets patients' referring_provider_id to NULL."""
    oid = current_org_id()
    prov = q_one("""SELECT first_name, last_name, clinic_id FROM referring_providers
                    WHERE id = ? AND organization_id = ?""", (provider_id, oid))
    if not prov: abort(404)
    q_exec('DELETE FROM referring_providers WHERE id = ? AND organization_id = ?',
           (provider_id, oid))
    flash(f'Provider {prov["first_name"]} {prov["last_name"]} deleted.', 'success')
    return redirect(url_for('referring_clinic_detail', clinic_id=prov['clinic_id']))


# ── Patients: referring assignment ───────────────────────────────────────────

@app.route('/patients/<int:patient_id>/referral', methods=['GET', 'POST'])
@require_login
def patient_referral_edit(patient_id):
    oid = current_org_id()
    p = q_one('SELECT * FROM patients WHERE id = ? AND organization_id = ?', (patient_id, oid))
    if not p: abort(404)
    clinics = q_all("""SELECT * FROM referring_clinics WHERE organization_id = ? AND is_active = 1
                       ORDER BY name""", (oid,))
    providers = q_all("""SELECT rp.*, c.name AS clinic_name FROM referring_providers rp
                         JOIN referring_clinics c ON c.id = rp.clinic_id
                         WHERE rp.organization_id = ? AND rp.is_active = 1
                         ORDER BY c.name, rp.last_name""", (oid,))
    if request.method == 'POST':
        clinic_id = request.form.get('referring_clinic_id', type=int) or None
        provider_id = request.form.get('referring_provider_id', type=int) or None
        # If provider set, enforce clinic consistency
        if provider_id:
            prov = q_one('SELECT clinic_id FROM referring_providers WHERE id = ? AND organization_id = ?',
                         (provider_id, oid))
            if not prov: abort(404)
            clinic_id = prov['clinic_id']
        q_exec("""UPDATE patients SET referring_clinic_id = ?, referring_provider_id = ?
                  WHERE id = ?""",
               (clinic_id, provider_id, patient_id))
        flash('Referral updated.', 'success')
        return redirect(url_for('patient_detail', patient_id=patient_id))
    return render_template('patient_referral_form.html', patient=p, clinics=clinics,
                           providers=providers)


# ── Template filters ─────────────────────────────────────────────────────────

@app.template_filter('dt')
def fmt_dt(s):
    """Format a datetime as DD-MMM-YYYY h:mm AM/PM (e.g. 24-Apr-2026 3:45 PM)."""
    if not s:
        return 'Never'
    if isinstance(s, str):
        try:
            s = datetime.fromisoformat(s.replace(' ', 'T'))
        except ValueError:
            return s
    return s.strftime('%d-%b-%Y ') + s.strftime('%I:%M %p').lstrip('0')


@app.template_filter('date_only')
def fmt_date_only(s):
    """Format a date as DD-MMM-YYYY (e.g. 24-Apr-2026)."""
    if not s:
        return '—'
    if isinstance(s, str):
        try:
            dt = datetime.fromisoformat(s.replace(' ', 'T')) if len(s) > 10 else datetime.strptime(s[:10], '%Y-%m-%d')
        except ValueError:
            return s
        return dt.strftime('%d-%b-%Y')
    return s.strftime('%d-%b-%Y')


@app.template_filter('time_only')
def fmt_time_only(s):
    """Format a datetime's time portion as h:mm AM/PM (e.g. 3:45 PM)."""
    if not s:
        return ''
    if isinstance(s, str):
        try:
            s = datetime.fromisoformat(s.replace(' ', 'T'))
        except ValueError:
            return s
    return s.strftime('%I:%M %p').lstrip('0')


@app.template_filter('adherence_class')
def adherence_class(pct):
    if pct is None: return 'status-unknown'
    if pct >= 80:  return 'status-green'
    if pct >= 50:  return 'status-yellow'
    return 'status-red'


@app.template_filter('model_label')
def model_label(m):
    return {'biwaze_cough': 'BiWaze Cough',
            'biwaze_clear': 'BiWaze Clear'}.get(m, m or '—')


# ── Run ──────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════
# RSS feed widget (floating, per-user configurable)
# ══════════════════════════════════════════════════════════════════════

RSS_FEEDS = [
    # --- Work / clinical (defaults for every user) ---
    {'key': 'fda_device_recalls', 'category': 'Work', 'icon': '⚠️',
     'name': 'FDA Device Recalls',
     'url': 'https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medical-devices/rss.xml',
     'default_on': True,
     'demo': [
        ('Philips Respironics recall expanded to include additional BiPAP models', '2026-04-20'),
        ('ResMed issues service advisory for CPAP transformer', '2026-04-15'),
        ('FDA strengthens labeling requirement for MI-E devices', '2026-04-08'),
     ]},
    {'key': 'cms', 'category': 'Work', 'icon': '🏛',
     'name': 'CMS Newsroom',
     'url': 'https://www.cms.gov/newsroom/rss-feed',
     'default_on': True,
     'demo': [
        ('CMS updates DMEPOS fee schedule for Q3 2026', '2026-04-19'),
        ('New guidance on capped rental documentation', '2026-04-12'),
     ]},
    {'key': 'rt_magazine', 'category': 'Work', 'icon': '🫁',
     'name': 'Respiratory Therapy Magazine',
     'url': 'https://respiratory-therapy.com/feed/',
     'default_on': True,
     'demo': [
        ('Airway clearance trends in home health: 2026 benchmark', '2026-04-18'),
        ('New research on MI-E adherence intervention protocols', '2026-04-11'),
     ]},
    # --- News ---
    {'key': 'npr', 'category': 'News', 'icon': '📰',
     'name': 'NPR Top Stories',
     'url': 'https://feeds.npr.org/1001/rss.xml',
     'default_on': True,
     'demo': [
        ('Federal Reserve holds rates steady in April meeting', '2026-04-23'),
        ('New infrastructure bill advances through Senate', '2026-04-22'),
        ('Tech industry employment rebounds in Q1', '2026-04-21'),
     ]},
    {'key': 'bbc', 'category': 'News', 'icon': '🌍',
     'name': 'BBC World News',
     'url': 'http://feeds.bbci.co.uk/news/world/rss.xml',
     'default_on': False,
     'demo': [
        ('EU summit closes with agreement on climate timeline', '2026-04-23'),
        ('UN reports progress on child health initiatives', '2026-04-22'),
     ]},
    # --- Weather ---
    {'key': 'noaa_co', 'category': 'Weather', 'icon': '🌤',
     'name': 'NOAA Alerts (Colorado)',
     'url': 'https://alerts.weather.gov/cap/co.php?x=1',
     'default_on': True,
     'demo': [
        ('No active weather alerts for Colorado', '2026-04-23'),
     ]},
    # --- Sports ---
    {'key': 'espn', 'category': 'Sports', 'icon': '⚽',
     'name': 'ESPN Top Headlines',
     'url': 'https://www.espn.com/espn/rss/news',
     'default_on': False,
     'demo': [
        ('NBA playoffs: conference semifinals preview', '2026-04-23'),
        ('MLB: opening month surprises so far', '2026-04-22'),
        ('NFL draft order finalized after trades', '2026-04-21'),
     ]},
    # --- Lifestyle ---
    {'key': 'nyt_cooking', 'category': 'Lifestyle', 'icon': '🍳',
     'name': 'NYT Cooking',
     'url': 'https://cooking.nytimes.com/rss.xml',
     'default_on': False,
     'demo': [
        ('15 spring pasta recipes to make this weekend', '2026-04-22'),
        ('Sheet-pan dinners for busy weeknights', '2026-04-20'),
     ]},
]
RSS_FEEDS_BY_KEY = {f['key']: f for f in RSS_FEEDS}

# In-memory cache: { feed_key: {'items': [...], 'fetched_at': dt, 'live': bool} }
_rss_cache = {}
_RSS_TTL_SECONDS = 15 * 60  # 15-minute cache


def _fetch_rss_items(url, max_items=4):
    """Fetch + parse RSS (or Atom) feed. Returns list of {title, link, date}
    or None on failure."""
    import xml.etree.ElementTree as ET
    from urllib.request import Request, urlopen
    try:
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0 Arc-Connect-Portal/1.0'})
        with urlopen(req, timeout=6) as resp:
            raw = resp.read(200_000)  # cap 200KB
        root = ET.fromstring(raw)
    except Exception:
        return None
    items = []
    # RSS 2.0: .//item
    for item in root.iter('item'):
        items.append({
            'title': (item.findtext('title') or '').strip()[:140],
            'link': (item.findtext('link') or '').strip(),
            'date': (item.findtext('pubDate') or '').strip()[:24],
        })
        if len(items) >= max_items: break
    # Atom fallback
    if not items:
        ns = '{http://www.w3.org/2005/Atom}'
        for entry in root.iter(f'{ns}entry'):
            title = entry.find(f'{ns}title')
            link = entry.find(f'{ns}link')
            updated = entry.find(f'{ns}updated')
            items.append({
                'title': ((title.text if title is not None else '') or '').strip()[:140],
                'link': (link.get('href') if link is not None else '') or '',
                'date': ((updated.text if updated is not None else '') or '').strip()[:24],
            })
            if len(items) >= max_items: break
    return items


def get_rss_items(feed_key, max_items=4):
    """Returns (items, is_live_data). Uses cache + falls back to demo content."""
    feed = RSS_FEEDS_BY_KEY.get(feed_key)
    if not feed: return ([], False)
    import time as _t
    cached = _rss_cache.get(feed_key)
    if cached and (_t.time() - cached['fetched_at']) < _RSS_TTL_SECONDS:
        return (cached['items'], cached['live'])
    live_items = _fetch_rss_items(feed['url'], max_items=max_items)
    if live_items:
        _rss_cache[feed_key] = {'items': live_items, 'fetched_at': _t.time(), 'live': True}
        return (live_items, True)
    # Fallback to demo content
    demo_items = [{'title': t, 'link': '#', 'date': d}
                  for (t, d) in feed.get('demo', [])][:max_items]
    _rss_cache[feed_key] = {'items': demo_items, 'fetched_at': _t.time(), 'live': False}
    return (demo_items, False)


def get_user_rss_keys():
    """Return the feed_keys the current user has enabled. Falls back to defaults."""
    u = current_user()
    if not u: return []
    raw = u['rss_feeds_json']
    if raw:
        try:
            keys = json.loads(raw)
            if isinstance(keys, list):
                return [k for k in keys if k in RSS_FEEDS_BY_KEY]
        except Exception:
            pass
    return [f['key'] for f in RSS_FEEDS if f.get('default_on')]


@app.context_processor
def inject_rss():
    u = current_user()
    if not u: return {}
    return {'user_rss_keys': get_user_rss_keys()}


@app.route('/api/rss')
@require_login
def api_rss():
    """Returns JSON with all subscribed feeds + their recent items."""
    keys = get_user_rss_keys()
    feeds_out = []
    for k in keys:
        meta = RSS_FEEDS_BY_KEY.get(k)
        if not meta: continue
        items, live = get_rss_items(k, max_items=4)
        feeds_out.append({
            'key': meta['key'], 'name': meta['name'],
            'category': meta['category'], 'icon': meta['icon'],
            'items': items, 'live': live,
        })
    return jsonify(feeds=feeds_out)


# ══════════════════════════════════════════════════════════════════════
# Tasks
# ══════════════════════════════════════════════════════════════════════

TASK_STATUS_LABELS = {
    'todo':             'To do',
    'in_progress':      'In progress',
    'pending_external': 'Pending external',
    'completed':        'Completed',
    'cancelled':        'Cancelled',
}

# Default due-date offsets (hours from creation) by alert severity
TASK_DUE_DEFAULTS = {'critical': 4, 'warning': 24, 'info': 72}

TASK_TEMPLATES = [
    'Call patient',
    'Order supplies',
    'Schedule follow-up visit',
    'Review therapy data',
    'Contact referring MD',
    'Check device / firmware',
    'Submit documentation',
    'Other',
]


def _task_notify(task_id, action):
    """Stub: in production this would send an email / SMS to the assignee
    using their configured notify_channel. Here we just log a line so the
    demo can demonstrate the notification flow."""
    task = q_one("""SELECT t.*, u.email, u.first_name, u.last_name, u.notify_channel,
                    u.notify_phone_e164, u.phone
                    FROM tasks t
                    LEFT JOIN users u ON u.id = t.assigned_to_user_id
                    WHERE t.id = ?""", (task_id,))
    if not task or not task['email']: return
    channel = task['notify_channel'] or 'email'
    if channel == 'none': return
    target_sms = task['notify_phone_e164'] or task['phone'] or '(no phone)'
    msg = f"[{action}] Task #{task_id}: {task['title']}"
    if channel in ('email', 'both'):
        app.logger.info(f"→ EMAIL to {task['email']}: {msg}")
    if channel in ('sms', 'both'):
        app.logger.info(f"→ SMS to {target_sms}: {msg}")


def _task_log(task_id, user_id, kind, detail):
    q_exec("""INSERT INTO task_activity (task_id, user_id, kind, detail)
              VALUES (?, ?, ?, ?)""", (task_id, user_id, kind, detail))


@app.route('/tasks')
@require_login
def tasks():
    u = current_user()
    scope_ids = scope_org_ids()
    rollup = is_rollup_scope()
    if not scope_ids:
        return render_template('tasks.html', tasks=[], view='my',
                               counts={'my':0,'open':0,'overdue':0,'pending_external':0,'completed':0},
                               status_labels=TASK_STATUS_LABELS,
                               assigned_alerts=[], bulk_users=[], rollup=rollup,
                               filter_location=0, locations=[])
    oid = scope_ids[0] if not rollup else None
    # Rollup-only location filter. Location users can't narrow by location
    # since they only see their own.
    filter_location = request.args.get('location', type=int) or 0
    if rollup and filter_location and filter_location in scope_ids:
        query_ids = [filter_location]
    elif rollup:
        filter_location = 0
        query_ids = scope_ids
    else:
        filter_location = 0
        query_ids = scope_ids
    ph = ','.join('?' * len(query_ids))
    view = request.args.get('view', 'my')  # my|open|overdue|pending_external|completed|all
    query = f"""SELECT t.*, p.first_name AS pt_first, p.last_name AS pt_last, p.mrn AS pt_mrn,
               u.first_name AS assignee_first, u.last_name AS assignee_last,
               a.severity AS alert_severity, a.message AS alert_message,
               ii.kind AS inbox_kind, ii.status AS inbox_status,
               o.id AS loc_id, o.name AS loc_name
               FROM tasks t
               JOIN organizations o ON o.id = t.organization_id
               LEFT JOIN patients p ON p.id = t.patient_id
               LEFT JOIN users u ON u.id = t.assigned_to_user_id
               LEFT JOIN alerts a ON a.id = t.alert_id
               LEFT JOIN inbox_items ii ON ii.id = t.inbox_item_id
               WHERE t.organization_id IN ({ph})"""
    params = list(query_ids)
    if view == 'my':
        query += " AND t.assigned_to_user_id = ? AND t.status NOT IN ('completed','cancelled')"
        params.append(u['id'])
    elif view == 'open':
        query += " AND t.status NOT IN ('completed','cancelled')"
    elif view == 'overdue':
        query += (" AND t.status NOT IN ('completed','cancelled') "
                  "AND t.due_at IS NOT NULL AND t.due_at < datetime('now')")
    elif view == 'pending_external':
        query += " AND t.status = 'pending_external'"
    elif view == 'completed':
        query += " AND t.status IN ('completed','cancelled')"
    # 'all' = no filter
    query += """ ORDER BY CASE t.status
                 WHEN 'todo' THEN 1 WHEN 'in_progress' THEN 2
                 WHEN 'pending_external' THEN 3 WHEN 'completed' THEN 4
                 WHEN 'cancelled' THEN 5 ELSE 6 END,
                 CASE t.priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                 t.due_at ASC NULLS LAST"""
    rows = q_all(query, tuple(params))

    # Tab counts — scoped to the currently-filtered org set so the counts
    # match the visible list.
    counts = q_one(f"""SELECT
        SUM(CASE WHEN assigned_to_user_id = ? AND status NOT IN ('completed','cancelled') THEN 1 ELSE 0 END) AS my,
        SUM(CASE WHEN status NOT IN ('completed','cancelled') THEN 1 ELSE 0 END) AS open,
        SUM(CASE WHEN status NOT IN ('completed','cancelled') AND due_at IS NOT NULL AND due_at < datetime('now') THEN 1 ELSE 0 END) AS overdue,
        SUM(CASE WHEN status = 'pending_external' THEN 1 ELSE 0 END) AS pending_external,
        SUM(CASE WHEN status IN ('completed','cancelled') THEN 1 ELSE 0 END) AS completed
        FROM tasks WHERE organization_id IN ({ph})""", (u['id'], *query_ids))

    # Messages are now regular tasks (auto-created when a message arrives),
    # so they appear in the tasks table via the inbox_item_id link. Alerts on
    # my patients still get a separate card since open alerts don't always
    # have a task attached.
    assigned_alerts = []
    if view == 'my':
        alert_rows = q_all(f"""SELECT a.id, a.severity, a.message, a.detail,
                              a.triggered_at, a.acknowledged_at,
                              p.id AS patient_id, p.first_name AS pt_first,
                              p.last_name AS pt_last, p.mrn AS pt_mrn,
                              (SELECT t.id FROM tasks t WHERE t.alert_id = a.id
                                 AND t.status NOT IN ('completed','cancelled') LIMIT 1) AS task_id
                              FROM alerts a
                              JOIN patients p ON p.id = a.patient_id
                              WHERE a.organization_id IN ({ph})
                                AND a.resolved_at IS NULL
                                AND (a.acknowledged_by_user_id = ?
                                     OR p.assigned_clinician_user_id = ?)
                              ORDER BY CASE a.severity
                                WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                                a.triggered_at DESC""",
                           (*query_ids, u['id'], u['id']))
        assigned_alerts = [dict(r) for r in alert_rows]
    bulk_users = q_all(f"""SELECT id, first_name, last_name FROM users
                          WHERE organization_id IN ({ph}) AND is_active = 1
                          ORDER BY last_name""", tuple(query_ids))
    # Locations list — only needed at rollup scope for the location filter.
    locations = []
    if rollup:
        loc_ph = ','.join('?' * len(scope_ids))
        locations = q_all(
            f"SELECT id, name FROM organizations WHERE id IN ({loc_ph}) ORDER BY name",
            tuple(scope_ids))
    return render_template('tasks.html', tasks=rows, view=view, counts=counts,
                           status_labels=TASK_STATUS_LABELS,
                           assigned_alerts=assigned_alerts,
                           bulk_users=bulk_users, rollup=rollup,
                           filter_location=filter_location, locations=locations)


@app.route('/tasks/new', methods=['GET', 'POST'])
@require_login
def task_new():
    oid = current_org_id()
    u = current_user()
    if request.method == 'POST':
        due_at = request.form.get('due_at') or None
        assignee = request.form.get('assigned_to_user_id', type=int) or u['id']
        patient_id = request.form.get('patient_id', type=int) or None
        task_id = q_exec("""INSERT INTO tasks (organization_id, patient_id, title, description,
                            status, priority, due_at, assigned_to_user_id, created_by_user_id)
                            VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, ?)""",
                         (oid, patient_id, request.form.get('title', '').strip(),
                          request.form.get('description') or None,
                          request.form.get('priority', 'normal'),
                          due_at, assignee, u['id']))
        _task_log(task_id, u['id'], 'created',
                  f"Task created and assigned to user #{assignee}")
        _task_notify(task_id, 'Assigned')
        flash('Task created.', 'success')
        return redirect(url_for('task_detail', task_id=task_id))
    users_list = q_all("""SELECT id, first_name, last_name, email, role FROM users
                          WHERE organization_id = ? AND is_active = 1
                          ORDER BY last_name""", (oid,))
    patients_list = q_all("""SELECT id, first_name, last_name, mrn FROM patients
                             WHERE organization_id = ? AND status = 'active'
                             ORDER BY last_name""", (oid,))
    prefill_patient_id = request.args.get('patient_id', type=int)
    return render_template('task_form.html', users=users_list, patients=patients_list,
                           templates=TASK_TEMPLATES, prefill_patient_id=prefill_patient_id)


@app.route('/tasks/<int:task_id>')
@require_login
def task_detail(task_id):
    oid = current_org_id()
    t = q_one("""SELECT t.*, p.first_name AS pt_first, p.last_name AS pt_last, p.mrn AS pt_mrn,
                 u.first_name AS assignee_first, u.last_name AS assignee_last, u.email AS assignee_email,
                 cu.first_name AS creator_first, cu.last_name AS creator_last,
                 a.severity AS alert_severity, a.message AS alert_message,
                 a.triggered_at AS alert_triggered_at
                 FROM tasks t
                 LEFT JOIN patients p ON p.id = t.patient_id
                 LEFT JOIN users u ON u.id = t.assigned_to_user_id
                 LEFT JOIN users cu ON cu.id = t.created_by_user_id
                 LEFT JOIN alerts a ON a.id = t.alert_id
                 WHERE t.id = ? AND t.organization_id = ?""", (task_id, oid))
    if not t: abort(404)
    activity = q_all("""SELECT ta.*, u.first_name, u.last_name
                        FROM task_activity ta
                        LEFT JOIN users u ON u.id = ta.user_id
                        WHERE ta.task_id = ? ORDER BY ta.occurred_at""", (task_id,))
    users_list = q_all("""SELECT id, first_name, last_name, role FROM users
                          WHERE organization_id = ? AND is_active = 1
                          ORDER BY last_name""", (oid,))
    # Pull the linked inbox item + message thread (if any) so message-tasks
    # render the conversation inline and support inline replies.
    inbox_item = None
    message_thread = []
    if t['inbox_item_id']:
        inbox_item = q_one('SELECT * FROM inbox_items WHERE id = ?', (t['inbox_item_id'],))
        if inbox_item and inbox_item['kind'] == 'message':
            root_msg = q_one('SELECT thread_id FROM patient_messages WHERE id = ?',
                             (inbox_item['ref_id'],))
            if root_msg:
                message_thread = q_all("""SELECT m.*, u.first_name AS author_first,
                                           u.last_name AS author_last
                                           FROM patient_messages m
                                           LEFT JOIN users u ON u.id = m.author_user_id
                                           WHERE m.thread_id = ?
                                           ORDER BY m.created_at""",
                                        (root_msg['thread_id'],))
    return render_template('task_detail.html', task=t, activity=activity,
                           users=users_list, status_labels=TASK_STATUS_LABELS,
                           inbox_item=inbox_item,
                           message_thread=message_thread)


@app.route('/tasks/<int:task_id>/status', methods=['POST'])
@require_login
def task_status(task_id):
    oid = current_org_id()
    u = current_user()
    t = q_one('SELECT * FROM tasks WHERE id = ? AND organization_id = ?', (task_id, oid))
    if not t: abort(404)
    new_status = request.form.get('status')
    if new_status not in TASK_STATUS_LABELS: abort(400)
    if new_status == t['status']:
        flash('Status unchanged.', 'info')
        return redirect(url_for('task_detail', task_id=task_id))
    # Complete or cancel: stamp completed_at/by
    if new_status in ('completed', 'cancelled'):
        q_exec("""UPDATE tasks SET status = ?, completed_at = CURRENT_TIMESTAMP,
                  completed_by_user_id = ? WHERE id = ?""",
               (new_status, u['id'], task_id))
        # Bidirectional: if task completed, auto-acknowledge linked alert
        if new_status == 'completed' and t['alert_id']:
            q_exec("""UPDATE alerts SET acknowledged_by_user_id = ?,
                      acknowledged_at = COALESCE(acknowledged_at, CURRENT_TIMESTAMP)
                      WHERE id = ? AND resolved_at IS NULL""",
                   (u['id'], t['alert_id']))
    else:
        q_exec("""UPDATE tasks SET status = ?, completed_at = NULL,
                  completed_by_user_id = NULL WHERE id = ?""", (new_status, task_id))

    # Sync to the linked inbox_item so /inbox reflects the same state.
    if t['inbox_item_id']:
        inbox_status = {
            'todo':             'unread',
            'in_progress':      'read',
            'pending_external': 'read',
            'completed':        'resolved',
            'cancelled':        'resolved',
        }.get(new_status, 'unread')
        if inbox_status == 'resolved':
            q_exec("""UPDATE inbox_items SET status = ?,
                      resolved_at = CURRENT_TIMESTAMP, resolved_by_user_id = ?
                      WHERE id = ?""", (inbox_status, u['id'], t['inbox_item_id']))
        else:
            q_exec("""UPDATE inbox_items SET status = ?,
                      resolved_at = NULL, resolved_by_user_id = NULL
                      WHERE id = ?""", (inbox_status, t['inbox_item_id']))

    _task_log(task_id, u['id'], 'status_changed',
              f"{t['status']} → {new_status}")
    flash(f'Status changed to {TASK_STATUS_LABELS[new_status]}.', 'success')
    return redirect(url_for('task_detail', task_id=task_id))


@app.route('/tasks/bulk', methods=['POST'])
@require_login
def task_bulk():
    """Bulk reassign / complete multiple tasks.
    Form: ids=[...]&action=reassign|complete&assigned_to_user_id=N"""
    oid = current_org_id()
    u = current_user()
    ids = request.form.getlist('ids', type=int)
    action = request.form.get('action')
    if not ids:
        flash('Select at least one task first.', 'error')
        return redirect(request.referrer or url_for('tasks'))
    placeholders = ','.join('?' * len(ids))
    if action == 'reassign':
        new_assignee = request.form.get('assigned_to_user_id', type=int)
        if not new_assignee: abort(400)
        assignee = q_one('SELECT id FROM users WHERE id = ? AND organization_id = ?',
                         (new_assignee, oid))
        if not assignee: abort(400)
        q_exec(f"""UPDATE tasks SET assigned_to_user_id = ?
                   WHERE id IN ({placeholders}) AND organization_id = ?""",
               (new_assignee,) + tuple(ids) + (oid,))
        # Sync assignee on any linked inbox items
        q_exec(f"""UPDATE inbox_items SET assigned_to_user_id = ?
                   WHERE id IN (SELECT inbox_item_id FROM tasks WHERE id IN ({placeholders}))
                   AND organization_id = ?""",
               (new_assignee,) + tuple(ids) + (oid,))
        flash(f'{len(ids)} task{"s" if len(ids) != 1 else ""} reassigned.', 'success')
    elif action == 'complete':
        q_exec(f"""UPDATE tasks SET status = 'completed',
                   completed_at = CURRENT_TIMESTAMP, completed_by_user_id = ?
                   WHERE id IN ({placeholders}) AND organization_id = ?
                   AND status NOT IN ('completed','cancelled')""",
               (u['id'],) + tuple(ids) + (oid,))
        flash(f'{len(ids)} task{"s" if len(ids) != 1 else ""} completed.', 'success')
    else:
        abort(400)
    return redirect(request.referrer or url_for('tasks'))


@app.route('/tasks/<int:task_id>/assign', methods=['POST'])
@require_login
def task_assign(task_id):
    oid = current_org_id()
    u = current_user()
    t = q_one('SELECT * FROM tasks WHERE id = ? AND organization_id = ?', (task_id, oid))
    if not t: abort(404)
    new_assignee_id = request.form.get('assigned_to_user_id', type=int)
    # Verify assignee is in same org
    assignee = q_one('SELECT id, first_name, last_name FROM users WHERE id = ? AND organization_id = ?',
                     (new_assignee_id, oid))
    if not assignee: abort(400)
    if t['assigned_to_user_id'] == new_assignee_id:
        flash('Task already assigned to that user.', 'info')
        return redirect(url_for('task_detail', task_id=task_id))
    q_exec("UPDATE tasks SET assigned_to_user_id = ? WHERE id = ?",
           (new_assignee_id, task_id))
    _task_log(task_id, u['id'], 'reassigned',
              f"Reassigned to {assignee['first_name']} {assignee['last_name']}")
    _task_notify(task_id, 'Reassigned')
    flash(f"Task reassigned to {assignee['first_name']} {assignee['last_name']}.",
          'success')
    return redirect(url_for('task_detail', task_id=task_id))


@app.route('/tasks/<int:task_id>/comment', methods=['POST'])
@require_login
def task_comment(task_id):
    oid = current_org_id()
    u = current_user()
    t = q_one('SELECT id FROM tasks WHERE id = ? AND organization_id = ?', (task_id, oid))
    if not t: abort(404)
    body = (request.form.get('body') or '').strip()
    if body:
        _task_log(task_id, u['id'], 'comment', body)
        flash('Comment added.', 'success')
    return redirect(url_for('task_detail', task_id=task_id))


@app.route('/alerts/<int:alert_id>/create-task', methods=['POST'])
@require_login
def alert_create_task(alert_id):
    """Inline 'Assign' button on alert cards — creates a task linked to the alert."""
    oid = current_org_id()
    u = current_user()
    a = q_one("""SELECT a.*, p.first_name, p.last_name, p.mrn FROM alerts a
                 JOIN patients p ON p.id = a.patient_id
                 WHERE a.id = ? AND a.organization_id = ?""", (alert_id, oid))
    if not a: abort(404)
    assignee_id = request.form.get('assigned_to_user_id', type=int) or u['id']
    # Default due from severity
    from datetime import datetime as _dt, timedelta as _td
    due_hours_default = TASK_DUE_DEFAULTS.get(a['severity'], 24)
    due_at = request.form.get('due_at')
    if not due_at:
        due_at = (_dt.now() + _td(hours=due_hours_default)).isoformat(sep=' ', timespec='minutes')
    title = (request.form.get('title') or f"Follow-up on alert: {a['message']}").strip()
    desc = request.form.get('description') or a['detail'] or ''
    priority = 'high' if a['severity'] == 'critical' else 'normal'
    task_id = q_exec("""INSERT INTO tasks (organization_id, patient_id, alert_id, title, description,
                        status, priority, due_at, assigned_to_user_id, created_by_user_id)
                        VALUES (?, ?, ?, ?, ?, 'todo', ?, ?, ?, ?)""",
                     (oid, a['patient_id'], alert_id, title, desc, priority,
                      due_at, assignee_id, u['id']))
    _task_log(task_id, u['id'], 'created',
              f"Task created from alert #{alert_id} — assigned to user #{assignee_id}")
    _task_notify(task_id, 'Assigned from alert')
    flash(f'Task created and assigned. Due {due_at}.', 'success')
    return redirect(request.referrer or url_for('alerts'))


@app.route('/my/profile', methods=['GET', 'POST'])
@require_login
def my_profile():
    u = current_user()
    if request.method == 'POST':
        notify_channel = request.form.get('notify_channel', 'email')
        if notify_channel not in ('email', 'sms', 'both', 'none'):
            notify_channel = 'email'
        q_exec("""UPDATE users SET notify_channel = ?,
                  phone = COALESCE(?, phone) WHERE id = ?""",
               (notify_channel,
                request.form.get('phone') or None,
                u['id']))
        flash('Profile updated.', 'success')
        return redirect(url_for('my_profile'))
    fresh = q_one("SELECT * FROM users WHERE id = ?", (u['id'],))
    return render_template('my_profile.html', user=fresh)


@app.route('/my/feeds', methods=['GET', 'POST'])
@require_login
def my_feeds():
    """Per-user RSS subscription settings. Any role can configure."""
    u = current_user()
    if request.method == 'POST':
        chosen = request.form.getlist('feeds')
        chosen = [k for k in chosen if k in RSS_FEEDS_BY_KEY]
        q_exec("UPDATE users SET rss_feeds_json = ? WHERE id = ?",
               (json.dumps(chosen), u['id']))
        flash('Feed subscriptions saved.', 'success')
        return redirect(url_for('my_feeds'))
    current_keys = set(get_user_rss_keys())
    # Group feeds by category
    by_cat = {}
    for f in RSS_FEEDS:
        by_cat.setdefault(f['category'], []).append({
            **f, 'enabled': f['key'] in current_keys,
        })
    return render_template('my_feeds.html', feeds_by_cat=by_cat,
                           total_enabled=len(current_keys))


# ══════════════════════════════════════════════════════════════════════
# Inbox — unified queue of messages, surveys, mood notes
# ══════════════════════════════════════════════════════════════════════

INBOX_STATUS_LABELS = TASK_STATUS_LABELS  # aliased — inbox and tasks share one status vocabulary now

INBOX_KIND_LABELS = {
    'message': 'Message',
    'survey':  'Survey',
    'mood':    'Mood note',
}

SURVEY_QUESTIONS = [
    ('q1_confidence',        'Confidence using the device'),
    ('q2_manageable',        'Schedule is manageable'),
    ('q3_breathing_better',  'Breathing has improved'),
    ('q4_tolerance',         'Comfort / tolerance'),
    ('q5_connected',         'Feels connected to care team'),
]


def _require_messaging_enabled():
    """Redirect to dashboard with a flash if the current location has
    messaging turned off (or the parent has overridden it)."""
    if not feature_enabled('messaging_enabled'):
        flash('Patient messaging is disabled for this location.', 'info')
        return redirect(url_for('dashboard'))
    return None


def _load_inbox_detail(row):
    """Attach the referenced source row (message / survey / mood) to an inbox row."""
    d = dict(row)
    if row['kind'] == 'message':
        m = q_one("""SELECT m.*, u.first_name AS author_first, u.last_name AS author_last
                     FROM patient_messages m
                     LEFT JOIN users u ON u.id = m.author_user_id
                     WHERE m.id = ?""", (row['ref_id'],))
        d['detail'] = dict(m) if m else {}
        # Thread history
        if m:
            thread = q_all("""SELECT m.*, u.first_name AS author_first, u.last_name AS author_last
                              FROM patient_messages m
                              LEFT JOIN users u ON u.id = m.author_user_id
                              WHERE m.thread_id = ? ORDER BY m.created_at""",
                           (m['thread_id'],))
            d['thread'] = [dict(t) for t in thread]
        else:
            d['thread'] = []
    elif row['kind'] == 'survey':
        s = q_one('SELECT * FROM patient_surveys WHERE id = ?', (row['ref_id'],))
        d['detail'] = dict(s) if s else {}
    elif row['kind'] == 'mood':
        m = q_one('SELECT * FROM patient_moods WHERE id = ?', (row['ref_id'],))
        d['detail'] = dict(m) if m else {}
    return d


@app.route('/inbox')
@require_login
def inbox():
    bounce = _require_messaging_enabled()
    if bounce: return bounce
    u = current_user()
    scope_ids = scope_org_ids()
    rollup = is_rollup_scope()
    if not scope_ids:
        return render_template('inbox.html', items=[],
                               counts={'open':0,'completed':0,'mine':0},
                               status='open', kind='all', mine=False,
                               status_labels=INBOX_STATUS_LABELS,
                               kind_labels=INBOX_KIND_LABELS,
                               users=[], rollup=rollup)
    ph = ','.join('?' * len(scope_ids))
    # Inbox reads status from the linked task whenever one exists (all items
    # seeded / created this way). The unified vocabulary is the task one:
    # todo / in_progress / pending_external / completed / cancelled.
    status = request.args.get('status', 'open')   # open|todo|in_progress|pending_external|completed|all
    kind   = request.args.get('kind',   'all')    # all|message|survey|mood
    mine   = request.args.get('mine') == '1'

    query = f"""SELECT i.id, i.kind, i.ref_id, i.created_at,
               i.patient_id,
               p.first_name, p.last_name, p.mrn,
               COALESCE(t.status, 'todo') AS status,
               t.id AS task_id, t.due_at,
               COALESCE(t.assigned_to_user_id, i.assigned_to_user_id) AS assigned_to_user_id,
               au.first_name AS assignee_first, au.last_name AS assignee_last,
               o.name AS loc_name
               FROM inbox_items i
               JOIN organizations o ON o.id = i.organization_id
               JOIN patients p ON p.id = i.patient_id
               LEFT JOIN tasks t ON t.inbox_item_id = i.id
               LEFT JOIN users au ON au.id = COALESCE(t.assigned_to_user_id, i.assigned_to_user_id)
               WHERE i.organization_id IN ({ph})"""
    params = list(scope_ids)
    if status == 'open':
        query += " AND COALESCE(t.status, 'todo') NOT IN ('completed','cancelled')"
    elif status != 'all':
        query += " AND COALESCE(t.status, 'todo') = ?"
        params.append(status)
    if kind != 'all':
        query += ' AND i.kind = ?';   params.append(kind)
    if mine:
        query += ' AND COALESCE(t.assigned_to_user_id, i.assigned_to_user_id) = ?'
        params.append(u['id'])
    query += """ ORDER BY
                 CASE COALESCE(t.status, 'todo')
                   WHEN 'todo' THEN 1 WHEN 'in_progress' THEN 2
                   WHEN 'pending_external' THEN 3 WHEN 'completed' THEN 4
                   WHEN 'cancelled' THEN 5 ELSE 6 END,
                 i.created_at DESC
                 LIMIT 200"""
    rows = q_all(query, tuple(params))
    items = [_load_inbox_detail(r) for r in rows]

    # Counts for tabs — derived from linked-task status
    counts = q_one(f"""SELECT
        SUM(CASE WHEN COALESCE(t.status, 'todo') NOT IN ('completed','cancelled') THEN 1 ELSE 0 END) AS open,
        SUM(CASE WHEN COALESCE(t.status, 'todo') = 'completed' THEN 1 ELSE 0 END) AS completed,
        SUM(CASE WHEN COALESCE(t.assigned_to_user_id, i.assigned_to_user_id) = ?
                  AND COALESCE(t.status, 'todo') NOT IN ('completed','cancelled') THEN 1 ELSE 0 END) AS mine
        FROM inbox_items i
        LEFT JOIN tasks t ON t.inbox_item_id = i.id
        WHERE i.organization_id IN ({ph})""", (u['id'], *scope_ids))
    users_list = q_all(f"""SELECT id, first_name, last_name FROM users
                          WHERE organization_id IN ({ph}) AND is_active = 1
                          ORDER BY last_name""", tuple(scope_ids))
    return render_template('inbox.html', items=items, counts=counts,
                           status=status, kind=kind, mine=mine,
                           status_labels=INBOX_STATUS_LABELS,
                           kind_labels=INBOX_KIND_LABELS,
                           users=users_list, rollup=rollup)


@app.route('/inbox/<int:item_id>/assign', methods=['POST'])
@require_login
def inbox_assign(item_id):
    bounce = _require_messaging_enabled()
    if bounce: return bounce
    oid = current_org_id()
    assignee = request.form.get('assigned_to_user_id', type=int) or None
    if assignee:
        u = q_one('SELECT id FROM users WHERE id = ? AND organization_id = ?',
                  (assignee, oid))
        if not u: abort(400)
    q_exec("""UPDATE inbox_items SET assigned_to_user_id = ?
              WHERE id = ? AND organization_id = ?""",
           (assignee, item_id, oid))
    # Sync the linked task's assignee (one source of truth for assignment)
    q_exec("""UPDATE tasks SET assigned_to_user_id = ?
              WHERE inbox_item_id = ? AND organization_id = ?""",
           (assignee, item_id, oid))
    flash('Inbox item assigned.' if assignee else 'Assignment cleared.', 'success')
    return redirect(request.referrer or url_for('inbox'))


@app.route('/inbox/<int:item_id>/status', methods=['POST'])
@require_login
def inbox_status(item_id):
    """Inbox delegates status to the linked task — single source of truth."""
    bounce = _require_messaging_enabled()
    if bounce: return bounce
    oid = current_org_id()
    new_status = request.form.get('status')
    if new_status not in TASK_STATUS_LABELS: abort(400)
    u = current_user()
    if new_status in ('completed', 'cancelled'):
        q_exec("""UPDATE tasks SET status = ?, completed_at = CURRENT_TIMESTAMP,
                  completed_by_user_id = ? WHERE inbox_item_id = ? AND organization_id = ?""",
               (new_status, u['id'], item_id, oid))
    else:
        q_exec("""UPDATE tasks SET status = ?, completed_at = NULL,
                  completed_by_user_id = NULL WHERE inbox_item_id = ? AND organization_id = ?""",
               (new_status, item_id, oid))
    flash(f'Marked {TASK_STATUS_LABELS[new_status].lower()}.', 'success')
    return redirect(request.referrer or url_for('inbox'))


@app.route('/inbox/<int:item_id>/reply', methods=['POST'])
@require_login
def inbox_reply(item_id):
    """Provider reply to a patient message thread."""
    bounce = _require_messaging_enabled()
    if bounce: return bounce
    oid = current_org_id()
    u = current_user()
    item = q_one('SELECT * FROM inbox_items WHERE id = ? AND organization_id = ?',
                 (item_id, oid))
    if not item or item['kind'] != 'message': abort(404)
    body = (request.form.get('body') or '').strip()
    if not body:
        flash('Reply cannot be empty.', 'error')
        return redirect(request.referrer or url_for('inbox'))
    msg = q_one('SELECT thread_id, patient_id FROM patient_messages WHERE id = ?',
                (item['ref_id'],))
    if not msg: abort(404)
    q_exec("""INSERT INTO patient_messages (organization_id, patient_id, thread_id,
              direction, author_user_id, body)
              VALUES (?, ?, ?, 'from_provider', ?, ?)""",
           (oid, msg['patient_id'], msg['thread_id'], u['id'], body))
    # A reply satisfies the "respond to patient" task — resolve both.
    q_exec("""UPDATE inbox_items SET status = 'resolved',
              resolved_at = CURRENT_TIMESTAMP, resolved_by_user_id = ?
              WHERE id = ?""", (u['id'], item_id))
    q_exec("""UPDATE tasks SET status = 'completed',
              completed_at = CURRENT_TIMESTAMP, completed_by_user_id = ?
              WHERE inbox_item_id = ?
              AND status NOT IN ('completed','cancelled')""",
           (u['id'], item_id))
    flash('Reply sent — task marked completed.', 'success')
    return redirect(request.referrer or url_for('inbox'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    if not DB_PATH.exists():
        print(f'Database not found at {DB_PATH}. Run: python seed.py')
        exit(1)
    app.run(host='127.0.0.1', port=port, debug=True)
