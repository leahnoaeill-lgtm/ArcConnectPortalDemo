#!/usr/bin/env python3
"""Heat-map demo backfill — idempotent.

Adds geographic diversity (2 new satellite locations: Houston + Atlanta) and
12 months of time-series data (alerts, surveys, referrals, discontinuations)
so the heat-map timelapse has meaningful data in every monthly bin.

Run after seed.py:
    python seed.py
    python backfill_heatmap_demo.py

Re-running is safe — the script detects its own marker satellites and skips.
"""
import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / 'arcconnect.db'
SENTINEL = 'HM-DEMO-2026-05'

random.seed(42)  # deterministic demo


def already_run(c):
    r = c.execute(
        "SELECT 1 FROM organizations WHERE name IN "
        "('Adapt — Houston', 'Adapt — Atlanta') LIMIT 1"
    ).fetchone()
    return r is not None


def find_parent_id(c):
    r = c.execute(
        "SELECT id FROM organizations WHERE name LIKE 'Adapt%' AND type='parent' LIMIT 1"
    ).fetchone()
    return r[0] if r else None


def add_satellites(c, parent_id):
    """Add Houston + Atlanta satellites. Returns dict of name → org_id."""
    new_orgs = {}
    rows = [
        ('Adapt — Houston', 'Houston', 'TX', '77002', 29.76, -95.37, 'America/Chicago',
         '1801 Smith St', '713-555-0801'),
        ('Adapt — Atlanta', 'Atlanta', 'GA', '30303', 33.75, -84.39, 'America/New_York',
         '270 Peachtree St NW', '404-555-0901'),
    ]
    # Only use columns guaranteed to exist in older seeded databases.
    org_cols = {r[1] for r in c.execute('PRAGMA table_info(organizations)')}
    has_lat_lon = {'latitude', 'longitude'}.issubset(org_cols)
    for name, city, state, zip_, lat, lon, tz, addr, phone in rows:
        if has_lat_lon:
            c.execute(
                """INSERT INTO organizations
                   (name, parent_id, type, status, address_line1, city, state, zip,
                    phone, timezone, latitude, longitude)
                   VALUES (?, ?, 'location', 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, parent_id, addr, city, state, zip_, phone, tz, lat, lon)
            )
        else:
            c.execute(
                """INSERT INTO organizations
                   (name, parent_id, type, status, address_line1, city, state, zip,
                    phone, timezone)
                   VALUES (?, ?, 'location', 'active', ?, ?, ?, ?, ?, ?)""",
                (name, parent_id, addr, city, state, zip_, phone, tz)
            )
        new_orgs[name] = c.lastrowid
    return new_orgs


def add_referring_clinic(c, org_id, name, city, state, zip_):
    c.execute(
        """INSERT INTO referring_clinics
           (organization_id, name, city, state, zip, is_active)
           VALUES (?, ?, ?, ?, ?, 1)""",
        (org_id, name, city, state, zip_)
    )
    return c.lastrowid


def add_patients(c, org_id, addresses, clinic_id):
    """Insert patients at this satellite. Returns list of new patient_ids."""
    pids = []
    diagnoses = ['ALS', 'Bronchiectasis', 'COPD', 'Cystic fibrosis',
                 'Muscular dystrophy', 'Spinal cord injury', 'Post-COVID']
    for i, (first, last, dob, addr, city, state, zip_) in enumerate(addresses):
        mrn = f'MRN-D{org_id}-{i:03d}'
        modalities = random.choice([['cough'], ['clear'], ['cough', 'clear']])
        recent_session = (datetime.now() - timedelta(days=random.randint(0, 5)))\
            .replace(microsecond=0).isoformat(sep=' ')
        c.execute(
            """INSERT INTO patients
               (organization_id, mrn, first_name, last_name, dob,
                address_line1, city, state, zip, status,
                rx_frequency_per_day, rx_modalities,
                adherence_pct_30d, last_session_at,
                mobile_app_enabled, diagnosis,
                referring_clinic_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
            (org_id, mrn, first, last, dob, addr, city, state, zip_,
             random.choice([2, 3, 4]),
             json.dumps(modalities),
             random.randint(48, 96),
             recent_session,
             random.choice([0, 1, 1, 1]),  # 75% on mobile app
             random.choice(diagnoses),
             clinic_id)
        )
        pids.append(c.lastrowid)
    return pids


# ───────────────────────── Time-series backfills ────────────────────────────

def first_of_month_n_back(n):
    """Date for the first of the month, n months back from today."""
    d = date.today().replace(day=1)
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
    return d


def days_in_month(d):
    nxt = (d + timedelta(days=32)).replace(day=1)
    return (nxt - d).days


def backfill_alerts(c, all_patient_ids, months=12):
    """5–10 alerts per month with mixed severity, mixed resolution."""
    rule = c.execute('SELECT id FROM alert_rules LIMIT 1').fetchone()
    rule_id = rule[0] if rule else None
    severities = (['warning'] * 5) + (['critical'] * 2) + (['info'] * 3)
    messages = [
        'Missed therapy 48h', 'Adherence below 60%', 'Device offline > 24h',
        'Low PCF (peak cough flow) trend', 'No mobile sync in 7 days',
        'Mood: poor (sad)', 'Settings deviation from prescription',
        'Session aborted mid-cycle', 'Device firmware update pending'
    ]
    n = 0
    for m_ago in range(months - 1, -1, -1):
        anchor = first_of_month_n_back(m_ago)
        span = days_in_month(anchor)
        for _ in range(random.randint(5, 10)):
            pid = random.choice(all_patient_ids)
            r = c.execute(
                'SELECT organization_id FROM patients WHERE id = ?', (pid,)
            ).fetchone()
            if not r:
                continue
            org_id = r[0]
            sev = random.choice(severities)
            triggered = datetime.combine(
                anchor + timedelta(days=random.randint(0, span - 1)),
                datetime.min.time()
            ) + timedelta(hours=random.randint(7, 21),
                          minutes=random.randint(0, 59))
            resolved_at = None
            if random.random() < 0.65:
                resolved_at = (triggered + timedelta(
                    hours=random.randint(2, 96))).isoformat(sep=' ')
            c.execute(
                """INSERT INTO alerts
                   (organization_id, patient_id, rule_id, triggered_at,
                    severity, metric_value, message, detail, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (org_id, pid, rule_id, triggered.isoformat(sep=' '),
                 sev, round(random.uniform(20, 95), 1),
                 random.choice(messages), SENTINEL, resolved_at)
            )
            n += 1
    return n


def backfill_surveys(c, all_patient_ids, months=12):
    """Spread completed 30/60/90-day surveys across months. UNIQUE(patient,milestone)
    means each patient can only have one row per milestone — when picking, skip
    duplicates silently."""
    n = 0
    for m_ago in range(months):
        anchor = first_of_month_n_back(m_ago)
        span = days_in_month(anchor)
        for _ in range(random.randint(3, 6)):
            pid = random.choice(all_patient_ids)
            r = c.execute(
                'SELECT organization_id FROM patients WHERE id = ?', (pid,)
            ).fetchone()
            if not r:
                continue
            org_id = r[0]
            milestone = random.choice([30, 60, 90])
            scores = [random.randint(2, 5) for _ in range(5)]
            completed = anchor + timedelta(days=random.randint(0, span - 1))
            try:
                c.execute(
                    """INSERT INTO patient_surveys
                       (organization_id, patient_id, milestone,
                        q1_confidence, q2_manageable, q3_breathing_better,
                        q4_tolerance, q5_connected, score_0_100,
                        sent_at, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (org_id, pid, milestone,
                     scores[0], scores[1], scores[2], scores[3], scores[4],
                     round(sum(scores) * 20 / 5), completed.isoformat(),
                     completed.isoformat())
                )
                n += 1
            except sqlite3.IntegrityError:
                # UNIQUE(patient_id, milestone) — patient already has this milestone.
                pass
    return n


def backfill_referrals(c, all_patient_ids, months=12):
    """3–7 referrals per month, attributed to the patient's existing referring
    clinic when present, otherwise any clinic at their org."""
    n = 0
    for m_ago in range(months - 1, -1, -1):
        anchor = first_of_month_n_back(m_ago)
        span = days_in_month(anchor)
        for _ in range(random.randint(3, 7)):
            pid = random.choice(all_patient_ids)
            r = c.execute(
                """SELECT p.organization_id, p.referring_clinic_id
                   FROM patients p WHERE p.id = ?""", (pid,)
            ).fetchone()
            if not r:
                continue
            org_id, rcid = r[0], r[1]
            if not rcid:
                rc = c.execute(
                    'SELECT id FROM referring_clinics WHERE organization_id = ? '
                    'AND is_active = 1 LIMIT 1', (org_id,)
                ).fetchone()
                if not rc:
                    continue
                rcid = rc[0]
            assigned = anchor + timedelta(days=random.randint(0, span - 1))
            c.execute(
                """INSERT INTO patient_referral_history
                   (organization_id, patient_id, clinic_id, assigned_at, reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (org_id, pid, rcid, assigned.isoformat(), SENTINEL)
            )
            n += 1
    return n


def backfill_discontinuations(c, all_patient_ids):
    """Mark 8 random patients as inactive at staggered past dates so the
    'Discontinuations this month' metric has data across many bins."""
    candidates = random.sample(all_patient_ids, min(8, len(all_patient_ids)))
    for i, pid in enumerate(candidates):
        m_ago = (i + 1)  # 1, 2, 3, ... 8 months back
        anchor = first_of_month_n_back(m_ago)
        last_session = anchor + timedelta(days=random.randint(0, 25))
        c.execute(
            "UPDATE patients SET status = 'inactive', last_session_at = ? "
            "WHERE id = ?",
            (datetime.combine(last_session, datetime.min.time())
                .isoformat(sep=' '), pid)
        )
    return len(candidates)


# ────────────────────────────── Main ────────────────────────────────────────

def main():
    if not DB_PATH.exists():
        print(f'Database not found at {DB_PATH}. Run seed.py first.')
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    c = conn.cursor()

    if already_run(c):
        print('Backfill already applied (Houston/Atlanta satellites exist). Skipping.')
        return

    parent_id = find_parent_id(c)
    if not parent_id:
        print('Adapt parent org not found — run seed.py first.')
        return

    print('Adding 2 new satellites (Houston, Atlanta)...')
    new_orgs = add_satellites(c, parent_id)

    # One referring clinic per new satellite so referral metrics work.
    hou_clinic = add_referring_clinic(
        c, new_orgs['Adapt — Houston'],
        'Houston Methodist Pulmonary', 'Houston', 'TX', '77030')
    atl_clinic = add_referring_clinic(
        c, new_orgs['Adapt — Atlanta'],
        'Emory Pulmonary & Sleep', 'Atlanta', 'GA', '30322')

    # Patients for Houston (varied Houston-area ZIPs in the 770/772 range).
    hou_addrs = [
        ('Marcus',  'Reed',     '1948-03-12', '1442 Westheimer Rd',  'Houston',     'TX', '77006'),
        ('Yolanda', 'Greene',   '1955-07-04', '8800 Bissonnet St',   'Houston',     'TX', '77074'),
        ('David',   'Patel',    '1962-11-22', '215 Travis St',       'Houston',     'TX', '77002'),
        ('Linda',   'Howard',   '1971-05-18', '5022 Memorial Dr',    'Houston',     'TX', '77007'),
        ('Bao',     'Nguyen',   '1944-09-30', '11410 Bellaire Blvd', 'Houston',     'TX', '77072'),
        ('Estela',  'Rivera',   '1958-02-14', '6610 Almeda Rd',      'Houston',     'TX', '77021'),
    ]
    hou_pids = add_patients(c, new_orgs['Adapt — Houston'], hou_addrs, hou_clinic)
    print(f'  Houston: {len(hou_pids)} patients')

    # Patients for Atlanta (303xx ZIPs).
    atl_addrs = [
        ('Tasha',   'Williams', '1959-09-14', '120 Peachtree St NE',     'Atlanta', 'GA', '30303'),
        ('Brian',   'Carter',   '1965-02-28', '850 Spring St NW',        'Atlanta', 'GA', '30308'),
        ('Sandra',  'Mitchell', '1973-12-09', '1245 Ponce de Leon Ave',  'Atlanta', 'GA', '30306'),
        ('Marcus',  'Hill',     '1944-04-16', '500 14th St NW',          'Atlanta', 'GA', '30318'),
        ('Janelle', 'Brooks',   '1967-08-22', '3344 Peachtree Rd',       'Atlanta', 'GA', '30326'),
    ]
    atl_pids = add_patients(c, new_orgs['Adapt — Atlanta'], atl_addrs, atl_clinic)
    print(f'  Atlanta: {len(atl_pids)} patients')

    all_pids = [r[0] for r in c.execute('SELECT id FROM patients').fetchall()]
    print(f'\nBackfilling 12 months of time-series for {len(all_pids)} total patients...')

    n_alerts = backfill_alerts(c, all_pids, months=12)
    print(f'  +{n_alerts} alerts')

    n_surveys = backfill_surveys(c, all_pids, months=12)
    print(f'  +{n_surveys} survey responses')

    n_refs = backfill_referrals(c, all_pids, months=12)
    print(f'  +{n_refs} referral history rows')

    n_disco = backfill_discontinuations(c, all_pids)
    print(f'  marked {n_disco} patients as discontinued at staggered past dates')

    conn.commit()
    conn.close()
    print('\nBackfill complete. Reload the heat map to see the timelapse populated.')


if __name__ == '__main__':
    main()
