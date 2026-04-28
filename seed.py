#!/usr/bin/env python3
"""Seed the Arc Connect Portal demo database.
Creates Adapt (parent) with 3 locations, a second independent org (Sunwest),
users at various levels, patients with narrative continuity from the PPT demo,
devices, assignments, alert rules, and triggered alerts.
"""

import json
import math
import os
import random
import sqlite3
from datetime import datetime, timedelta, date, time as dtime
from pathlib import Path

THERAPY_GOAL_PER_DAY = 2  # per prescribed modality per day
SESSION_DAYS = 45         # how many days of history to seed

HERE = Path(__file__).parent
DB_PATH = HERE / 'arcconnect.db'
SCHEMA_PATH = HERE / 'schema.sql'
UPLOADS_LOGOS = HERE / 'static' / 'uploads' / 'logos'
UPLOADS_CONSENT = HERE / 'static' / 'uploads' / 'consent'

# Deterministic RNG so re-seeding produces the same serial numbers
_SERIAL_RNG = random.Random(20260423)

def _gen_serial(model):
    """Generate a serial number matching the product mask.
    BiWaze Cough → CNSXXAXXX, BiWaze Clear → KNSXXAXXX (X = digit)."""
    prefix = 'C' if model == 'biwaze_cough' else 'K' if model == 'biwaze_clear' else 'C'
    d = lambda: str(_SERIAL_RNG.randint(0, 9))
    return f"{prefix}NS{d()}{d()}A{d()}{d()}{d()}"


def _remask_serials(rows):
    """Replace the first field (serial) of each tuple with a mask-conformant one.
    Tuples are (serial, model, fw, ...) — we only change position 0."""
    return [(_gen_serial(t[1]),) + t[1:] for t in rows]


def init_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    UPLOADS_LOGOS.mkdir(parents=True, exist_ok=True)
    UPLOADS_CONSENT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn


def iso(dt):
    return dt.isoformat(sep=' ', timespec='seconds') if dt else None


def seed():
    conn = init_db()
    c = conn.cursor()
    now = datetime.now()

    # ── Organizations ───────────────────────────────────────────────
    # ABMRC internal (super admin tenant — covered by BAA with each customer)
    c.execute("""INSERT INTO organizations (name, parent_id, type, status, phone, email,
                 address_line1, city, state, zip, timezone)
                 VALUES (?, NULL, 'internal', 'active', ?, ?, ?, ?, ?, ?, ?)""",
              ('ABM Respiratory Care', '1-800-ABMRC-00', 'support@abmrespiratory.com',
               '500 Industrial Dr', 'Indianapolis', 'IN', '46214', 'America/Indianapolis'))
    abmrc_id = c.lastrowid

    # Adapt parent (Chicago HQ)
    c.execute("""INSERT INTO organizations (name, parent_id, type, phone, email,
                 address_line1, city, state, zip, timezone, latitude, longitude)
                 VALUES (?, NULL, 'parent', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              ('Adapt Respiratory', '1-800-ADAPT-001', 'info@adapt.com',
               '1200 Corporate Pkwy', 'Chicago', 'IL', '60601', 'America/Chicago',
               41.8781, -87.6298))
    adap_id = c.lastrowid

    # Adapt — Denver
    c.execute("""INSERT INTO organizations (name, parent_id, type, phone, email,
                 address_line1, city, state, zip, timezone, latitude, longitude)
                 VALUES (?, ?, 'location', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              ('Adapt — Denver', adap_id, '303-555-0140', 'denver@adapt.com',
               '4455 Colfax Ave', 'Denver', 'CO', '80220', 'America/Denver',
               39.7392, -104.9903))
    adap_denver = c.lastrowid

    # Adapt — Boulder
    c.execute("""INSERT INTO organizations (name, parent_id, type, phone, email,
                 address_line1, city, state, zip, timezone, latitude, longitude)
                 VALUES (?, ?, 'location', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              ('Adapt — Boulder', adap_id, '303-555-0212', 'boulder@adapt.com',
               '2201 Pearl St', 'Boulder', 'CO', '80302', 'America/Denver',
               40.0150, -105.2705))
    adap_boulder = c.lastrowid

    # Adapt — Phoenix
    c.execute("""INSERT INTO organizations (name, parent_id, type, phone, email,
                 address_line1, city, state, zip, timezone, latitude, longitude)
                 VALUES (?, ?, 'location', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              ('Adapt — Phoenix', adap_id, '602-555-0318', 'phoenix@adapt.com',
               '88 W Camelback Rd', 'Phoenix', 'AZ', '85013', 'America/Phoenix',
               33.4484, -112.0740))
    adap_phoenix = c.lastrowid

    # Sunwest (independent org, separate tenant)
    c.execute("""INSERT INTO organizations (name, parent_id, type, phone, email,
                 address_line1, city, state, zip, timezone, latitude, longitude)
                 VALUES (?, NULL, 'location', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              ('Sunwest Medical', '480-555-0700', 'info@sunwest.com',
               '2200 E Baseline Rd', 'Tempe', 'AZ', '85283', 'America/Phoenix',
               33.4255, -111.9400))
    sunwest_id = c.lastrowid

    # ── Users ────────────────────────────────────────────────────────
    # ABMRC super admin
    c.execute("""INSERT INTO users (organization_id, email, first_name, last_name,
                 role, phone) VALUES (?, ?, ?, ?, 'super_admin', ?)""",
              (abmrc_id, 'support@abmrespiratory.com', 'Casey', 'Morgan', '317-555-0101'))
    super_admin_id = c.lastrowid

    # ── Verification on file for the two active customer orgs ──────────
    today = date.today()
    c.execute("""UPDATE organizations SET verification_complete = 1,
                 verification_date = ?, verification_notes = ?,
                 verified_by_user_id = ?, npi = ?
                 WHERE id = ?""",
              ((today - timedelta(days=180)).isoformat(),
               'NPI verified via NPPES; multi-location BAA reviewed; CCO reference (Karen Hughes) confirmed by phone.',
               super_admin_id, '1487629034', adap_id))
    c.execute("""UPDATE organizations SET verification_complete = 1,
                 verification_date = ?, verification_notes = ?,
                 verified_by_user_id = ?, npi = ?
                 WHERE id = ?""",
              ((today - timedelta(days=92)).isoformat(),
               'NPI verified via NPPES; AZ HME license on file; primary contact (Maria Torres) reached and validated.',
               super_admin_id, '1730495812', sunwest_id))

    # ── Sample BAA on file for Adapt Respiratory (signed last year, valid 2y) ──
    c.execute("""INSERT INTO organization_baas (organization_id, file_path, file_name,
                 signed_date, effective_from, expires_on,
                 signed_by_name, signed_by_title)
                 VALUES (?, 'uploads/baas/sample_adapt_baa.pdf', 'Adapt_BAA_2025.pdf',
                         ?, ?, ?, 'Karen Hughes', 'Chief Compliance Officer')""",
              (adap_id, (today.replace(year=today.year - 1)).isoformat(),
               (today.replace(year=today.year - 1)).isoformat(),
               (today.replace(year=today.year + 1)).isoformat()))

    # ── Sample BAA on file for Sunwest Medical (signed ~6 months ago, valid 2y) ──
    sunwest_signed = (today - timedelta(days=180)).isoformat()
    sunwest_expires = (today + timedelta(days=550)).isoformat()
    c.execute("""INSERT INTO organization_baas (organization_id, file_path, file_name,
                 signed_date, effective_from, expires_on,
                 signed_by_name, signed_by_title)
                 VALUES (?, 'uploads/baas/sample_sunwest_baa.pdf', 'Sunwest_BAA_2025.pdf',
                         ?, ?, ?, 'Maria Torres', 'Owner / Director')""",
              (sunwest_id, sunwest_signed, sunwest_signed, sunwest_expires))

    # Adapt HQ
    c.execute("""INSERT INTO users (organization_id, email, first_name, last_name,
                 role, phone) VALUES (?, ?, ?, ?, 'admin', ?)""",
              (adap_id, 'karen.h@adapt.com', 'Karen', 'Hughes', '312-555-0100'))

    # Adapt Denver users
    denver_users = [
        ('priya.s@adapt.com', 'Priya', 'Shah', 'admin', '303-555-0141'),
        ('james.r@adapt.com', 'James', 'Reyes', 'clinician', '303-555-0142'),
        ('alex.k@adapt.com', 'Alex', 'Kim', 'clinician', '303-555-0143'),
        ('sam.l@adapt.com', 'Sam', 'Liu', 'billing', '303-555-0144'),
        ('devi.p@adapt.com', 'Devi', 'Patel', 'clinician', '303-555-0145'),
    ]
    denver_user_ids = {}
    for email, fn, ln, role, phone in denver_users:
        c.execute("""INSERT INTO users (organization_id, email, first_name, last_name, role, phone)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (adap_denver, email, fn, ln, role, phone))
        denver_user_ids[email] = c.lastrowid

    # Adapt Boulder
    c.execute("""INSERT INTO users (organization_id, email, first_name, last_name, role, phone)
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (adap_boulder, 'linda.w@adapt.com', 'Linda', 'Wong', 'admin', '303-555-0213'))
    c.execute("""INSERT INTO users (organization_id, email, first_name, last_name, role, phone)
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (adap_boulder, 'tom.g@adapt.com', 'Tom', 'Garcia', 'clinician', '303-555-0214'))

    # Adapt Phoenix
    c.execute("""INSERT INTO users (organization_id, email, first_name, last_name, role, phone)
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (adap_phoenix, 'carlos.m@adapt.com', 'Carlos', 'Morales', 'admin', '602-555-0319'))

    # Sunwest
    c.execute("""INSERT INTO users (organization_id, email, first_name, last_name, role, phone)
                 VALUES (?, ?, ?, ?, ?, ?)""",
              (sunwest_id, 'maria.t@sunwest.com', 'Maria', 'Torres', 'admin', '480-555-0701'))

    # ── Patients (Adapt Denver — the main demo location) ─────────────
    # Seed patterned after the PPT clickable demo
    # target_adherence is the DESIRED 30-day adherence — we'll generate sessions
    # to approximately hit this, then recompute adherence_pct_30d from actual sessions.
    patients_denver = [
        # mrn, first, last, dob, lang, rx_freq, modalities, rental_start,
        #   clinician_email, target_adherence, diagnosis
        ('MRN-448239', 'Maria',    'Garcia',    '1948-03-12', 'es-US', 3,
         ['cough','clear'], 180,  'priya.s@adapt.com',     0, 'ALS (neuromuscular respiratory weakness)'),
        ('MRN-44829',  'John',     'Smith',     '1952-08-04', 'en-US', 2,
         ['cough'],         95,   'james.r@adapt.com',    28, 'COPD with recurrent pneumonia'),
        ('MRN-51022',  'Liu',      'Chen',      '1960-11-22', 'en-US', 2,
         ['clear'],         45,   'alex.k@adapt.com',     62, 'Bronchiectasis'),
        ('MRN-44118',  'Mary',     'Jones',     '1955-02-17', 'en-US', 2,
         ['cough'],         220,  'james.r@adapt.com',    71, 'Post-polio syndrome'),
        ('MRN-49203',  'Sarah',    'Wilson',    '1968-06-30', 'en-US', 3,
         ['clear'],         120,  'devi.p@adapt.com',     95, 'Cystic fibrosis'),
        ('MRN-47885',  'Michael',  'Brown',     '1945-09-09', 'en-US', 2,
         ['cough','clear'], 75,   'devi.p@adapt.com',     86, 'Muscular dystrophy (Duchenne)'),
        ('MRN-48990',  'Robert',   'Davis',     '1958-04-14', 'en-US', 3,
         ['cough','clear'], 210,  'james.r@adapt.com',   100, 'ALS (late-stage)'),
        ('MRN-50011',  'Ravi',     'Patel',     '1950-12-05', 'en-US', 2,
         ['cough'],         130,  'devi.p@adapt.com',     93, 'Cervical spinal cord injury (C4)'),
        ('MRN-50188',  'Arjun',    'Kumar',     '1972-01-28', 'en-US', 2,
         ['cough','clear'], 30,   'alex.k@adapt.com',     88, 'Spinal muscular atrophy (type II)'),
        ('MRN-50245',  'Evelyn',   'Wright',    '1940-11-11', 'en-US', 1,
         ['clear'],         260,  'priya.s@adapt.com',    77, 'Chronic bronchitis with mucus retention'),
        ('MRN-50401',  'Haruto',   'Tanaka',    '1963-07-19', 'en-US', 2,
         ['cough'],         88,   'devi.p@adapt.com',     91, 'Myotonic dystrophy'),
        ('MRN-50512',  'Sophia',   'Nguyen',    '1988-03-03', 'en-US', 3,
         ['cough','clear'], 14,   'alex.k@adapt.com',     55, 'Traumatic brain injury with respiratory compromise'),
    ]
    patient_ids_denver = {}
    patient_target_adherence = {}
    patient_modalities = {}
    for (mrn, fn, ln, dob, lang, freq, mods, rent_off, clinician_email,
         adhere, diagnosis) in patients_denver:
        clinician_id = denver_user_ids.get(clinician_email)
        rent_start = (date.today() - timedelta(days=rent_off)).isoformat()
        rent_end = (date.today() - timedelta(days=rent_off) + timedelta(days=13*30)).isoformat()
        c.execute("""INSERT INTO patients (organization_id, mrn, first_name, last_name, dob,
                     preferred_language, rx_frequency_per_day, rx_modalities, capped_rental_start,
                     capped_rental_end, assigned_clinician_user_id, diagnosis, adherence_pct_30d)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, mrn.replace('MRN-', ''), fn, ln, dob, lang, freq, json.dumps(mods),
                   rent_start, rent_end, clinician_id, diagnosis, adhere))
        pid = c.lastrowid
        patient_ids_denver[mrn] = pid
        patient_target_adherence[pid] = adhere
        patient_modalities[pid] = mods

    # Boulder patients (small set)
    boulder_patients = [
        ('MRN-B-0012', 'Henrik', 'Olsen',   '1956-05-20', 2, ['cough'],            85, 90, 'Post-stroke dysphagia with aspiration risk'),
        ('MRN-B-0034', 'Priscilla','Vance', '1970-10-02', 1, ['clear'],            180, 82, 'Primary ciliary dyskinesia'),
        ('MRN-B-0051', 'Miguel',  'Ortiz',  '1961-06-01', 3, ['cough','clear'],    55, 94, 'ALS (early-stage)'),
    ]
    patient_ids_boulder = {}
    for mrn, fn, ln, dob, freq, mods, rent_off, adhere, diagnosis in boulder_patients:
        rent_start = (date.today() - timedelta(days=rent_off)).isoformat()
        rent_end = (date.today() - timedelta(days=rent_off) + timedelta(days=13*30)).isoformat()
        c.execute("""INSERT INTO patients (organization_id, mrn, first_name, last_name, dob,
                     rx_frequency_per_day, rx_modalities, capped_rental_start,
                     capped_rental_end, diagnosis, adherence_pct_30d)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_boulder, mrn.replace('MRN-', ''), fn, ln, dob, freq, json.dumps(mods),
                   rent_start, rent_end, diagnosis, adhere))
        pid = c.lastrowid
        patient_ids_boulder[mrn] = pid
        patient_target_adherence[pid] = adhere
        patient_modalities[pid] = mods

    # Sunwest patients
    patient_ids_sunwest = {}
    for mrn, fn, ln, diagnosis, target in [
        ('MRN-S-001','Carlos','Rivera','Bronchiectasis', 80),
        ('MRN-S-002','Amelia','Stone','COPD', 75),
    ]:
        c.execute("""INSERT INTO patients (organization_id, mrn, first_name, last_name, dob,
                     rx_frequency_per_day, rx_modalities, diagnosis, adherence_pct_30d)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (sunwest_id, mrn.replace('MRN-', ''), fn, ln, '1955-01-01', 2,
                   json.dumps(['cough']), diagnosis, target))
        pid = c.lastrowid
        patient_ids_sunwest[mrn] = pid
        patient_target_adherence[pid] = target
        patient_modalities[pid] = ['cough']

    # ── Referring clinics + providers ───────────────────────────────
    # Denver clinics
    denver_clinics = [
        ('Denver Pulmonary Associates', '1234567890', '303-555-2100',
         'referrals@denverpulm.com', 'https://denverpulmonary.com',
         '1600 E 19th Ave', 'Denver', 'CO', '80218'),
        ('Rocky Mountain Neuromuscular Center', '1234567891', '303-555-3200',
         'intake@rmnmc.com', 'https://rmnmcenter.com',
         '12605 E 16th Ave', 'Aurora', 'CO', '80045'),
        ('Front Range Pediatrics', '1234567892', '303-555-4300',
         'office@frpeds.com', 'https://frontrangepeds.com',
         '3555 S Colorado Blvd', 'Englewood', 'CO', '80113'),
    ]
    denver_clinic_ids = {}
    for name, npi, phone, email, url, addr, city, state, zip_ in denver_clinics:
        c.execute("""INSERT INTO referring_clinics (organization_id, name, npi, phone, email,
                     website_url, address_line1, city, state, zip)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, name, npi, phone, email, url, addr, city, state, zip_))
        denver_clinic_ids[name] = c.lastrowid

    # Denver providers (keyed by last name for easy patient linkage)
    denver_providers = [
        # (clinic_name, first, last, credentials, specialty, npi, phone, email)
        ('Denver Pulmonary Associates', 'James',   'Chen',      'MD',  'Pulmonology',           '1111111111', '303-555-2101', 'j.chen@denverpulm.com'),
        ('Denver Pulmonary Associates', 'Sarah',   'Okonkwo',   'MD',  'Pulmonology',           '1111111112', '303-555-2102', 's.okonkwo@denverpulm.com'),
        ('Denver Pulmonary Associates', 'Maya',    'Patel',     'NP',  'Pulmonology',           '1111111113', '303-555-2103', 'm.patel@denverpulm.com'),
        ('Rocky Mountain Neuromuscular Center', 'Eric', 'Lindholm', 'MD', 'Neuromuscular medicine', '2222222221', '303-555-3201', 'e.lindholm@rmnmc.com'),
        ('Rocky Mountain Neuromuscular Center', 'Nadia', 'Rahman',   'MD', 'Neurology',             '2222222222', '303-555-3202', 'n.rahman@rmnmc.com'),
        ('Front Range Pediatrics', 'Thomas',  'Webb',      'MD',  'Pediatric pulmonology', '3333333331', '303-555-4301', 't.webb@frpeds.com'),
    ]
    denver_provider_ids = {}  # keyed by last name
    for clinic_name, fn, ln, cred, spec, npi, phone, email in denver_providers:
        c.execute("""INSERT INTO referring_providers (organization_id, clinic_id, first_name, last_name,
                     credentials, specialty, npi, phone, email)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, denver_clinic_ids[clinic_name], fn, ln, cred, spec, npi, phone, email))
        denver_provider_ids[ln] = c.lastrowid

    # Boulder clinics
    boulder_clinics = [
        ('Boulder Respiratory Medicine', '4444444441', '303-555-5100',
         'referrals@boulderresp.com', 'https://boulderresp.com',
         '4725 Arapahoe Ave', 'Boulder', 'CO', '80303'),
        ('Flatirons Neurology', '4444444442', '303-555-6200',
         'intake@flatironsneuro.com', 'https://flatironsneuro.com',
         '2525 Arapahoe Ave', 'Boulder', 'CO', '80302'),
    ]
    boulder_clinic_ids = {}
    for name, npi, phone, email, url, addr, city, state, zip_ in boulder_clinics:
        c.execute("""INSERT INTO referring_clinics (organization_id, name, npi, phone, email,
                     website_url, address_line1, city, state, zip)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_boulder, name, npi, phone, email, url, addr, city, state, zip_))
        boulder_clinic_ids[name] = c.lastrowid

    boulder_providers = [
        ('Boulder Respiratory Medicine', 'Priya',  'Bhattacharya', 'MD', 'Pulmonology', '5555555551', '303-555-5101', 'p.b@boulderresp.com'),
        ('Boulder Respiratory Medicine', 'Daniel', 'Foster',       'MD', 'Pulmonology', '5555555552', '303-555-5102', 'd.foster@boulderresp.com'),
        ('Flatirons Neurology',          'Rachel', 'Kim',          'MD', 'Neuromuscular medicine', '6666666661', '303-555-6201', 'r.kim@flatironsneuro.com'),
    ]
    boulder_provider_ids = {}
    for clinic_name, fn, ln, cred, spec, npi, phone, email in boulder_providers:
        c.execute("""INSERT INTO referring_providers (organization_id, clinic_id, first_name, last_name,
                     credentials, specialty, npi, phone, email)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_boulder, boulder_clinic_ids[clinic_name], fn, ln, cred, spec, npi, phone, email))
        boulder_provider_ids[ln] = c.lastrowid

    # Sunwest — one clinic, one provider
    c.execute("""INSERT INTO referring_clinics (organization_id, name, npi, phone, email,
                 website_url, address_line1, city, state, zip)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (sunwest_id, 'Phoenix Pulmonary Partners', '7777777771', '480-555-8100',
               'referrals@phxpulm.com', 'https://phxpulmonary.com',
               '3003 N 3rd St', 'Phoenix', 'AZ', '85012'))
    sunwest_clinic_id = c.lastrowid
    c.execute("""INSERT INTO referring_providers (organization_id, clinic_id, first_name, last_name,
                 credentials, specialty, npi, phone, email)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (sunwest_id, sunwest_clinic_id, 'Antonio', 'Ruiz', 'MD', 'Pulmonology',
               '8888888881', '480-555-8101', 'a.ruiz@phxpulm.com'))

    # ── Link existing patients to clinics/providers ─────────────────
    # Denver patient → provider mapping
    denver_pt_refs = {
        'MRN-448239': ('Chen',      'Denver Pulmonary Associates'),          # Maria Garcia
        'MRN-44829':  ('Okonkwo',   'Denver Pulmonary Associates'),          # John Smith
        'MRN-51022':  ('Lindholm',  'Rocky Mountain Neuromuscular Center'),  # Liu Chen
        'MRN-44118':  ('Chen',      'Denver Pulmonary Associates'),          # Mary Jones
        'MRN-49203':  ('Patel',     'Denver Pulmonary Associates'),          # Sarah Wilson
        'MRN-47885':  ('Rahman',    'Rocky Mountain Neuromuscular Center'),  # Michael Brown
        'MRN-48990':  ('Chen',      'Denver Pulmonary Associates'),          # Robert Davis
        'MRN-50011':  ('Okonkwo',   'Denver Pulmonary Associates'),          # Ravi Patel
        'MRN-50188':  ('Lindholm',  'Rocky Mountain Neuromuscular Center'),  # Arjun Kumar
        'MRN-50245':  ('Chen',      'Denver Pulmonary Associates'),          # Evelyn Wright
        'MRN-50401':  ('Patel',     'Denver Pulmonary Associates'),          # Haruto Tanaka
        'MRN-50512':  ('Webb',      'Front Range Pediatrics'),               # Sophia Nguyen
    }
    for mrn, (provider_last, clinic_name) in denver_pt_refs.items():
        pid = patient_ids_denver.get(mrn)
        if not pid: continue
        c.execute("""UPDATE patients SET referring_clinic_id = ?, referring_provider_id = ?
                     WHERE id = ?""",
                  (denver_clinic_ids[clinic_name], denver_provider_ids[provider_last], pid))

    # Boulder patient → provider mapping
    boulder_pt_refs = {
        'MRN-B-0012': ('Foster',       'Boulder Respiratory Medicine'),  # Henrik Olsen
        'MRN-B-0034': ('Kim',          'Flatirons Neurology'),           # Priscilla Vance
        'MRN-B-0051': ('Bhattacharya', 'Boulder Respiratory Medicine'),  # Miguel Ortiz
    }
    for mrn, (provider_last, clinic_name) in boulder_pt_refs.items():
        pid = patient_ids_boulder.get(mrn)
        if not pid: continue
        c.execute("""UPDATE patients SET referring_clinic_id = ?, referring_provider_id = ?
                     WHERE id = ?""",
                  (boulder_clinic_ids[clinic_name], boulder_provider_ids[provider_last], pid))

    # ── Devices (Adapt Denver — the main demo) ────────────────────────
    # Seeded with realistic serial numbers + mix of assigned/unassigned
    denver_devices = [
        # serial, model, firmware, upload_days_ago, last_comm_hours_ago (None = never),
        #   warranty_end_days_ahead, status, assign_to_mrn, assign_days_ago
        ('BC-2024-0045821', 'biwaze_cough', '3.4.1', 195, 8,     1100, 'in_use',   'MRN-448239', 180),
        ('BCL-2024-0012003','biwaze_clear', '2.9.4', 195, 36,    1100, 'in_use',   'MRN-448239', 180),
        ('BC-2024-0045820', 'biwaze_cough', '3.4.0', 100, 72,    1050, 'in_use',   'MRN-44829',  95),
        ('BC-2024-0045819', 'biwaze_cough', '3.4.1', 50,  28,    1400, 'in_use',   'MRN-51022',  45),
        ('BC-2023-0041102', 'biwaze_cough', '3.2.8', 730, None,  30,   'maintenance', None, None),
        ('BCL-2024-0012012','biwaze_clear', '2.9.4', 30,  None,  1400, 'in_stock', None, None),
        ('BC-2024-0045800', 'biwaze_cough', '3.4.1', 35,  6,     1200, 'in_use',   'MRN-50188',  30),
        ('BC-2022-0038219', 'biwaze_cough', '3.2.5', 1100, 7,    -45,  'in_use',   'MRN-49203',  120),
        ('BCL-2024-0012105','biwaze_clear', '2.9.4', 80,  5,     1180, 'in_use',   'MRN-47885',  75),
        ('BC-2024-0045777', 'biwaze_cough', '3.4.1', 80,  5,     1180, 'in_use',   'MRN-47885',  75),
        ('BC-2024-0045765', 'biwaze_cough', '3.4.1', 225, 4,     1000, 'in_use',   'MRN-48990',  210),
        ('BCL-2024-0011998','biwaze_clear', '2.9.4', 225, 3,     1000, 'in_use',   'MRN-48990',  210),
        ('BC-2024-0045850', 'biwaze_cough', '3.4.1', 10,  None,  1450, 'in_stock', None, None),
        ('BCL-2024-0012222','biwaze_clear', '2.9.4', 10,  None,  1450, 'in_stock', None, None),
        ('BC-2024-0045855', 'biwaze_cough', '3.4.1', 5,   None,  1455, 'in_stock', None, None),
        ('BC-2024-0045700', 'biwaze_cough', '3.4.1', 145, 26,    1220, 'in_use',   'MRN-44118',  220),
        ('BC-2024-0045695', 'biwaze_cough', '3.4.1', 135, 22,    1200, 'in_use',   'MRN-50245',  260),
        ('BCL-2024-0011880','biwaze_clear', '2.9.4', 270, 22,    1095, 'in_use',   'MRN-50245',  260),
        ('BC-2024-0045690', 'biwaze_cough', '3.4.1', 95,  6,     1290, 'in_use',   'MRN-50401',  88),
        ('BC-2024-0045680', 'biwaze_cough', '3.4.1', 18,  40,    1400, 'in_use',   'MRN-50512',  14),
        ('BCL-2024-0012150','biwaze_clear', '2.9.4', 18,  40,    1400, 'in_use',   'MRN-50512',  14),
    ]
    denver_devices = _remask_serials(denver_devices)
    device_id_by_serial = {}
    assignment_queue = []
    for (serial, model, fw, upload_off, last_comm_hrs, warranty_days, status,
         assign_mrn, assign_off) in denver_devices:
        upload_date = (date.today() - timedelta(days=upload_off)).isoformat()
        last_comm = (now - timedelta(hours=last_comm_hrs)).isoformat(sep=' ', timespec='seconds') if last_comm_hrs is not None else None
        warranty_end = (date.today() + timedelta(days=warranty_days)).isoformat()
        c.execute("""INSERT INTO devices (organization_id, serial_number, model, firmware_version,
                     upload_date, last_communication, warranty_end, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, serial, model, fw, upload_date, last_comm, warranty_end, status))
        device_id_by_serial[serial] = c.lastrowid
        if assign_mrn and assign_off is not None:
            assignment_queue.append(('denver', serial, assign_mrn, assign_off))

    # Boulder devices
    boulder_devices = [
        # serial, model, fw, upload_off, last_comm_hrs, warranty_days, status, assign_to_mrn, assign_days_ago
        ('BC-2024-0048001', 'biwaze_cough', '3.4.1', 95, 8, 1270, 'in_use',   'MRN-B-0012', 85),   # Henrik Olsen
        ('BC-2024-0048002', 'biwaze_cough', '3.4.1', 90, 15, 1275, 'in_use',   'MRN-B-0051', 55),   # Miguel Ortiz
        ('BCL-2024-0013101','biwaze_clear', '2.9.4', 60, 3, 1305, 'in_use',   'MRN-B-0034', 180),  # Priscilla Vance
        ('BC-2024-0048050', 'biwaze_cough', '3.4.1', 5, None, 1460, 'in_stock', None, None),
    ]
    boulder_devices = _remask_serials(boulder_devices)
    for serial, model, fw, upload_off, hrs, wr, status, assign_mrn, assign_off in boulder_devices:
        upload_date = (date.today() - timedelta(days=upload_off)).isoformat()
        last_comm = (now - timedelta(hours=hrs)).isoformat(sep=' ', timespec='seconds') if hrs is not None else None
        warranty_end = (date.today() + timedelta(days=wr)).isoformat()
        c.execute("""INSERT INTO devices (organization_id, serial_number, model, firmware_version,
                     upload_date, last_communication, warranty_end, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_boulder, serial, model, fw, upload_date, last_comm, warranty_end, status))
        device_id_by_serial[serial] = c.lastrowid
        if assign_mrn and assign_off is not None:
            assignment_queue.append(('boulder', serial, assign_mrn, assign_off))

    # Sunwest devices
    sunwest_devices = [
        ('SW-2024-0001', 'biwaze_cough', '3.4.1', 50, 10, 1400, 'in_use',   'MRN-S-001', 45),  # Carlos Rivera
        ('SW-2024-0002', 'biwaze_cough', '3.4.1', 40, None, 1420, 'in_stock', None, None),
    ]
    sunwest_devices = _remask_serials(sunwest_devices)
    for serial, model, fw, upload_off, hrs, wr, status, assign_mrn, assign_off in sunwest_devices:
        upload_date = (date.today() - timedelta(days=upload_off)).isoformat()
        last_comm = (now - timedelta(hours=hrs)).isoformat(sep=' ', timespec='seconds') if hrs is not None else None
        warranty_end = (date.today() + timedelta(days=wr)).isoformat()
        c.execute("""INSERT INTO devices (organization_id, serial_number, model, firmware_version,
                     upload_date, last_communication, warranty_end, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (sunwest_id, serial, model, fw, upload_date, last_comm, warranty_end, status))
        device_id_by_serial[serial] = c.lastrowid
        if assign_mrn and assign_off is not None:
            assignment_queue.append(('sunwest', serial, assign_mrn, assign_off))

    # ── Device assignments (with placeholder consent forms) ────────
    priya_id = denver_user_ids.get('priya.s@adapt.com')
    patient_lookup = {
        'denver': patient_ids_denver,
        'boulder': patient_ids_boulder,
        'sunwest': patient_ids_sunwest,
    }
    for location_key, serial, mrn, assign_off in assignment_queue:
        device_id = device_id_by_serial[serial]
        patient_id = patient_lookup[location_key].get(mrn)
        if not patient_id:
            continue  # safety — skip if patient wasn't seeded
        assigned_date = (date.today() - timedelta(days=assign_off)).isoformat()
        # Write placeholder consent file
        consent_name = f'consent_{mrn}_{serial}.pdf'
        consent_relpath = f'uploads/consent/{consent_name}'
        consent_fullpath = UPLOADS_CONSENT / consent_name
        if not consent_fullpath.exists():
            consent_fullpath.write_bytes(
                b'%PDF-1.4\n% Arc Connect demo placeholder consent form\n'
                b'% In production this would be a signed PDF from the patient.\n'
                + f'% Patient MRN: {mrn}  Device: {serial}\n'.encode()
            )
        c.execute("""INSERT INTO device_assignments (patient_id, device_id, assigned_date,
                     consent_form_path, consent_form_original_name, assigned_by_user_id, notes)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (patient_id, device_id, assigned_date, consent_relpath,
                   f'{mrn}_consent.pdf', priya_id, 'Seeded at home setup visit.'))

    # ── Alert rules (Adapt Denver defaults) ─────────────────────────
    rules = [
        ('Missed therapy (1 day)', 'missed_therapy_days', 1, 24, 'warning',
         'Patient has not completed a prescribed therapy session for 1 day.'),
        ('Missed therapy (2 days)', 'missed_therapy_days', 2, 48, 'critical',
         'Patient has not completed a prescribed therapy session for 2 days.'),
        ('Device offline (12h)', 'device_disconnected_hours', 12, 12, 'warning',
         'Device has not synced data in 12 hours.'),
        ('Device offline (48h)', 'device_disconnected_hours', 48, 48, 'critical',
         'Device has not synced data in 48 hours — investigate device or patient.'),
        ('Adherence drop (30d)', 'adherence_pct_drop', 20, 720, 'warning',
         'Patient 30-day adherence dropped 20% or more compared to prior 30 days.'),
    ]
    rule_ids = {}
    for name, metric, threshold, window, severity, desc in rules:
        c.execute("""INSERT INTO alert_rules (organization_id, name, description, metric,
                     threshold_value, window_hours, severity, notify_email, notify_in_app,
                     notify_recipient_roles)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)""",
                  (adap_denver, name, desc, metric, threshold, window, severity,
                   json.dumps(['admin','clinician'])))
        rule_ids[name] = c.lastrowid

    # Adapt parent org — seed the same default rule set so the parent-managed
    # policy option isn't empty if the group admin flips the toggle.
    for name, metric, threshold, window, severity, desc in rules:
        c.execute("""INSERT INTO alert_rules (organization_id, name, description, metric,
                     threshold_value, window_hours, severity, notify_email, notify_in_app,
                     notify_recipient_roles)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)""",
                  (adap_id, name, desc, metric, threshold, window, severity,
                   json.dumps(['admin','clinician'])))

    # Boulder — copy two default rules
    for name, metric, threshold, window, severity, desc in rules[:2]:
        c.execute("""INSERT INTO alert_rules (organization_id, name, description, metric,
                     threshold_value, window_hours, severity, notify_email, notify_in_app,
                     notify_recipient_roles)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)""",
                  (adap_boulder, name, desc, metric, threshold, window, severity,
                   json.dumps(['admin','clinician'])))

    # Sunwest — copy one rule
    c.execute("""INSERT INTO alert_rules (organization_id, name, description, metric,
                 threshold_value, window_hours, severity, notify_email, notify_in_app,
                 notify_recipient_roles)
                 VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)""",
              (sunwest_id, 'Missed therapy (2 days)', 'Default rule',
               'missed_therapy_days', 2, 48, 'critical',
               json.dumps(['admin'])))

    # ── Seed addresses + phone/email for a subset of patients ──────
    patient_contact_info = {
        'MRN-448239': ('303-555-0201', 'maria.garcia@example.com',
                       '1842 Elm St', None, 'Denver', 'CO', '80220'),
        'MRN-44829':  ('303-555-0202', None,
                       '227 Maple Ave', 'Apt 4B', 'Aurora', 'CO', '80012'),
        'MRN-51022':  ('303-555-0203', 'liu.chen@example.com',
                       '5510 Dahlia Way', None, 'Denver', 'CO', '80220'),
        'MRN-47885':  ('303-555-0204', None,
                       '3318 Sheridan Blvd', None, 'Wheat Ridge', 'CO', '80033'),
        'MRN-48990':  ('303-555-0205', 'r.davis@example.com',
                       '89 Lakeshore Dr', None, 'Arvada', 'CO', '80002'),
        'MRN-50011':  ('303-555-0206', None,
                       '1020 Grant St', 'Unit 12', 'Denver', 'CO', '80203'),
        'MRN-50188':  ('303-555-0207', 'arjun.k@example.com',
                       '445 Logan St', None, 'Denver', 'CO', '80206'),
        'MRN-50512':  ('303-555-0208', None,
                       '620 Quebec St', None, 'Denver', 'CO', '80220'),
        'MRN-B-0012': ('303-555-0301', None,
                       '1250 Broadway', None, 'Boulder', 'CO', '80302'),
        'MRN-B-0034': ('303-555-0302', 'p.vance@example.com',
                       '2050 Iris Ave', None, 'Boulder', 'CO', '80304'),
    }
    for mrn, (phone, email, a1, a2, city, st, zip_) in patient_contact_info.items():
        pid = all_patient_maps.get(mrn) if 'all_patient_maps' in dir() else None
        # We haven't computed all_patient_maps yet at this point — use individual maps
        pid = (patient_ids_denver.get(mrn) or patient_ids_boulder.get(mrn)
               or patient_ids_sunwest.get(mrn))
        if not pid: continue
        c.execute("""UPDATE patients SET phone = COALESCE(?, phone), email = COALESCE(?, email),
                     address_line1 = ?, address_line2 = ?, city = ?, state = ?, zip = ?
                     WHERE id = ?""",
                  (phone, email, a1, a2, city, st, zip_, pid))

    # ── Insurance + prescriptions ───────────────────────────────────
    # Lookup patient → referring provider id so we can attribute the Rx
    patient_provider_id = {}
    for row in c.execute("""SELECT id, referring_provider_id FROM patients
                            WHERE referring_provider_id IS NOT NULL""").fetchall():
        patient_provider_id[row['id']] = row['referring_provider_id']

    # Realistic payer mixes — most HME respiratory patients are Medicare-eligible
    insurance_plans = {
        'MRN-448239': ('Medicare',                'Medicare Part B',  'MED-448239-A', None,         180, 172),  # Maria Garcia — ALS
        'MRN-44829':  ('UnitedHealthcare',        'Commercial PPO',   'UHC-JS-0429',  '88421-UHC',  95,  92),
        'MRN-51022':  ('Anthem Blue Cross',       'Commercial HMO',   'ANT-LC-0822',  '72104-ANT',  45,  40),
        'MRN-44118':  ('Medicare',                'Medicare Part B',  'MED-MJ-0217',  None,         220, 210),
        'MRN-49203':  ('Aetna',                   'Commercial PPO',   'AET-SW-0630',  '55301-AET',  120, 115),
        'MRN-47885':  ('Medicare',                'Medicare Part B',  'MED-MB-0909',  None,         75,  70),
        'MRN-48990':  ('Medicare',                'Medicare Part B',  'MED-RD-0414',  None,         210, 204),
        'MRN-50011':  ('Medicaid — CO',           'Medicaid',         'MCO-RP-1205',  'MCD-RP-11',  130, 125),
        'MRN-50188':  ('Blue Cross Blue Shield',  'Commercial PPO',   'BCBS-AK-0128', '61120-BCB',  30,  25),
        'MRN-50245':  ('Medicare',                'Medicare Part B',  'MED-EW-1111',  None,         260, 255),
        'MRN-50401':  ('Kaiser Permanente',       'Commercial HMO',   'KAI-HT-0719',  '44118-KAI',  88,  80),
        'MRN-50512':  ('Tricare',                 'Military',         'TRI-SN-0303',  'T-SN-3303',  14,  7),
        # Boulder
        'MRN-B-0012': ('Medicare',                'Medicare Part B',  'MED-HO-0520',  None,         85,  80),
        'MRN-B-0034': ('Anthem Blue Cross',       'Commercial PPO',   'ANT-PV-1002',  '38841-ANT',  180, 172),
        'MRN-B-0051': ('Medicare',                'Medicare Part B',  'MED-MO-0601',  None,         55,  50),
        # Sunwest
        'MRN-S-001':  ('Medicare',                'Medicare Part B',  'MED-CR-0101',  None,         100, 95),
        'MRN-S-002':  ('UnitedHealthcare',        'Commercial PPO',   'UHC-AS-0615',  '77221-UHC',  140, 135),
    }
    all_patient_maps = {}
    all_patient_maps.update(patient_ids_denver)
    all_patient_maps.update(patient_ids_boulder)
    all_patient_maps.update(patient_ids_sunwest)

    for mrn, pid in all_patient_maps.items():
        plan = insurance_plans.get(mrn)
        if not plan: continue
        payer, plan_type, member_id, auth, eff_off, appr_off = plan
        effective = (date.today() - timedelta(days=eff_off)).isoformat()
        base_approval = (date.today() - timedelta(days=appr_off)).isoformat()
        next_review = (date.today() + timedelta(days=180)).isoformat()
        # Per-device approvals. For dual-modality patients the Clear approval
        # typically lands ~5 days after the Cough approval (payers process
        # them separately); single-modality patients only get their one.
        mods = patient_modalities.get(pid, [])
        cough_approval = base_approval if 'cough' in mods else None
        cough_auth     = auth if 'cough' in mods else None
        clear_approval = ((date.today() - timedelta(days=max(0, appr_off - 5))).isoformat()
                          if 'clear' in mods else None)
        clear_auth     = ((auth + '-CL') if auth and 'clear' in mods else (auth if 'clear' in mods else None))
        c.execute("""INSERT INTO patient_insurance
                     (patient_id, payer_name, plan_type, member_id, coverage_type,
                      effective_date, cough_approval_date, cough_auth_number,
                      clear_approval_date, clear_auth_number, next_review_date)
                     VALUES (?, ?, ?, ?, 'primary', ?, ?, ?, ?, ?, ?)""",
                  (pid, payer, plan_type, member_id, effective,
                   cough_approval, cough_auth, clear_approval, clear_auth,
                   next_review))

    # Prescriptions — per patient per prescribed modality
    # Clinically typical defaults with mild per-patient variation
    cough_defaults = {
        'cycles': 5,
        'insp_pressure': 40.0,
        'insp_time': 2.0,
        'exp_pressure': -40.0,
        'exp_time': 2.0,
        'pause_pressure': 0.0,
        'pause_time': 1.0,
    }
    clear_defaults = {
        'pep_pressure': 15.0,
        'pep_time': 120.0,
        'osc_pressure': 25.0,
        'osc_time': 90.0,
        'neb_enabled': 1,
    }
    rx_per_patient_variation = {
        # mrn: (insp_pressure override, exp_pressure override, cycles override)
        'MRN-448239': (45.0, -45.0, 5),   # Maria — ALS needs high pressures
        'MRN-44829':  (35.0, -35.0, 4),   # John — COPD, gentler
        'MRN-48990':  (50.0, -50.0, 6),   # Robert — late-stage ALS
        'MRN-50188':  (42.0, -42.0, 5),   # Arjun
        'MRN-50011':  (40.0, -40.0, 5),   # Ravi
        'MRN-50512':  (38.0, -38.0, 5),   # Sophia — TBI
    }
    clear_variation = {
        # mrn: (pep_p, osc_p, neb_on, neb_medication, bleed_in_o2, o2_flow_lpm)
        'MRN-49203': (18.0, 28.0, 1, '7% hypertonic saline',    0, None),   # Sarah Wilson — CF
        'MRN-51022': (15.0, 25.0, 1, '3% hypertonic saline',    0, None),   # Liu Chen — bronchiectasis
        'MRN-50245': (12.0, 20.0, 0, None,                      1, 2.0),    # Evelyn — gentler, bleed-in O2
        'MRN-B-0034':(16.0, 26.0, 1, 'Albuterol 2.5 mg + 7% HTS', 0, None), # Priscilla — PCD, combo neb
    }

    now_iso = (date.today() - timedelta(days=30)).isoformat()
    for mrn, pid in all_patient_maps.items():
        mods = patient_modalities.get(pid, [])
        provider_id = patient_provider_id.get(pid)
        # Prescription date should precede device assignment — use rental_start - ~5 days
        p_row = c.execute('SELECT capped_rental_start FROM patients WHERE id = ?', (pid,)).fetchone()
        if p_row and p_row['capped_rental_start']:
            rx_date = (date.fromisoformat(p_row['capped_rental_start'])
                       - timedelta(days=5)).isoformat()
        else:
            rx_date = (date.today() - timedelta(days=40)).isoformat()
        for m in mods:
            if m == 'cough':
                override = rx_per_patient_variation.get(mrn, (None, None, None))
                insp_p = override[0] or cough_defaults['insp_pressure']
                exp_p = override[1] or cough_defaults['exp_pressure']
                cycles = override[2] or cough_defaults['cycles']
                c.execute("""INSERT INTO patient_prescriptions
                             (patient_id, modality, prescribed_by_provider_id,
                              prescribed_date, effective_date,
                              cough_cycles, cough_insp_pressure_cmh2o, cough_insp_time_sec,
                              cough_exp_pressure_cmh2o, cough_exp_time_sec,
                              cough_pause_pressure_cmh2o, cough_pause_time_sec,
                              is_active)
                             VALUES (?, 'cough', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                          (pid, provider_id, rx_date, now_iso,
                           cycles, insp_p, cough_defaults['insp_time'],
                           exp_p, cough_defaults['exp_time'],
                           cough_defaults['pause_pressure'], cough_defaults['pause_time']))
            else:  # clear
                override = clear_variation.get(mrn, (None, None, None, None, 0, None))
                pep_p = override[0] or clear_defaults['pep_pressure']
                osc_p = override[1] or clear_defaults['osc_pressure']
                neb = override[2] if override[2] is not None else clear_defaults['neb_enabled']
                neb_med = override[3]
                bleed = override[4] if override[4] is not None else 0
                o2_flow = override[5]
                c.execute("""INSERT INTO patient_prescriptions
                             (patient_id, modality, prescribed_by_provider_id,
                              prescribed_date, effective_date,
                              clear_pep_pressure_cmh2o, clear_pep_time_sec,
                              clear_osc_pressure_cmh2o, clear_osc_time_sec,
                              clear_neb_enabled, clear_neb_medication,
                              clear_bleed_in_oxygen, clear_oxygen_flow_lpm, is_active)
                             VALUES (?, 'clear', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                          (pid, provider_id, rx_date, now_iso,
                           pep_p, clear_defaults['pep_time'],
                           osc_p, clear_defaults['osc_time'], neb, neb_med,
                           bleed, o2_flow))

    # ── Therapy sessions + waveforms ────────────────────────────────
    random.seed(42)

    def gen_cough_waveform(duration_seconds=240, cycles=5):
        """Generate a Cough session pressure waveform: 5 cycles of insp ramp to +40,
        hold, then exp ramp to -40, pause. Returns list of pressure samples."""
        samples_per_cycle = 30  # 150 pts / 5 cycles
        samples = []
        for _ in range(cycles):
            peak_insp = random.uniform(38, 42)
            peak_exp = random.uniform(-42, -38)
            # Inspiratory ramp (9 samples)
            for i in range(9):
                samples.append(round(peak_insp * (i + 1) / 9, 2))
            # Hold (3 samples)
            for _ in range(3):
                samples.append(round(peak_insp + random.uniform(-1, 1), 2))
            # Expiratory ramp (9 samples)
            for i in range(9):
                frac = (i + 1) / 9
                samples.append(round(peak_insp + (peak_exp - peak_insp) * frac, 2))
            # Hold exp (3 samples)
            for _ in range(3):
                samples.append(round(peak_exp + random.uniform(-1, 1), 2))
            # Pause (6 samples)
            for _ in range(6):
                samples.append(round(random.uniform(-0.5, 0.5), 2))
        sample_rate_hz = len(samples) / duration_seconds
        return {'sample_rate_hz': round(sample_rate_hz, 3),
                'duration_seconds': duration_seconds,
                'samples': samples}

    def gen_clear_waveform(duration_seconds=420):
        """Generate a Clear session: PEP (120s, ~15 cmH2O) → OSC (90s, sinusoid 15±10)
        → NEB (60s, low) → PEP (120s) → OSC (30s). 150 samples total."""
        samples = []
        # PEP 1 — 30 samples around 15 cmH2O with respiratory oscillation
        for i in range(30):
            samples.append(round(15 + 3 * math.sin(i * 0.5) + random.uniform(-0.5, 0.5), 2))
        # OSC 1 — 25 samples sinusoidal 25±10
        for i in range(25):
            samples.append(round(25 + 10 * math.sin(i * 1.5) + random.uniform(-1, 1), 2))
        # NEB — 20 samples low
        for _ in range(20):
            samples.append(round(random.uniform(-0.5, 2), 2))
        # PEP 2
        for i in range(30):
            samples.append(round(15 + 3 * math.sin(i * 0.5) + random.uniform(-0.5, 0.5), 2))
        # OSC 2 — tighter
        for i in range(15):
            samples.append(round(25 + 10 * math.sin(i * 1.5) + random.uniform(-1, 1), 2))
        # Wind-down
        for i in range(30):
            samples.append(round((15 - i * 0.5) + random.uniform(-0.5, 0.5), 2))
        sample_rate_hz = len(samples) / duration_seconds
        return {'sample_rate_hz': round(sample_rate_hz, 3),
                'duration_seconds': duration_seconds,
                'samples': samples}

    # Find the primary device per patient per modality for realistic linkage
    patient_cough_device = {}
    patient_clear_device = {}
    for row in c.execute("""SELECT da.patient_id, d.id AS device_id, d.model
                            FROM device_assignments da
                            JOIN devices d ON d.id = da.device_id
                            WHERE da.returned_date IS NULL""").fetchall():
        if row['model'] == 'biwaze_cough':
            patient_cough_device[row['patient_id']] = row['device_id']
        elif row['model'] == 'biwaze_clear':
            patient_clear_device[row['patient_id']] = row['device_id']

    # Generate sessions per patient.
    # Subset of patients also track SpO2 and/or Heart Rate via a paired
    # pulse-ox. Only those patients' therapy sessions get vitals populated.
    all_patient_ids = list(patient_target_adherence.keys())
    vitals_tracking = {}
    for pid in all_patient_ids:
        roll = random.random()
        if roll < 0.25:
            vitals_tracking[pid] = ('spo2', 'hr')
        elif roll < 0.45:
            vitals_tracking[pid] = ('spo2',)
        elif roll < 0.55:
            vitals_tracking[pid] = ('hr',)
        else:
            vitals_tracking[pid] = ()
    session_rows = []
    for pid in all_patient_ids:
        target = patient_target_adherence[pid]
        mods = patient_modalities[pid]
        tracks = vitals_tracking[pid]
        for days_back in range(SESSION_DAYS):
            day = date.today() - timedelta(days=days_back)
            # Recent 7 days get a slightly lower realization for variety; older more stable
            recency_factor = 0.85 if days_back < 7 else 1.0
            day_adh = target * recency_factor / 100.0  # target 0-1
            for modality in mods:
                # How many sessions today? goal is THERAPY_GOAL_PER_DAY
                # Probability of each slot completed = day_adh
                for slot in range(THERAPY_GOAL_PER_DAY):
                    if random.random() > day_adh:
                        continue  # skipped
                    # Generate session
                    # Session start time: morning (~9:00) or evening (~20:00)
                    hour = 9 + random.randint(-1, 1) if slot == 0 else 20 + random.randint(-1, 1)
                    minute = random.randint(0, 55)
                    started_at = datetime.combine(day, dtime(hour % 24, minute))
                    # Skip future-dated sessions
                    if started_at > now:
                        continue
                    if modality == 'cough':
                        duration = random.randint(180, 300)
                        wf = gen_cough_waveform(duration_seconds=duration, cycles=5)
                        peak_pressure = max(wf['samples'])
                        peak_flow = round(random.uniform(180, 260), 1)
                        # Insufflation volume (mL) — rough clinical range 600-1500
                        volume_ml = round(peak_flow * 4.5 + random.uniform(-120, 120), 0)
                        mode = 'auto'
                        device_id = patient_cough_device.get(pid)
                    else:
                        duration = random.randint(360, 480)
                        wf = gen_clear_waveform(duration_seconds=duration)
                        peak_pressure = max(wf['samples'])
                        peak_flow = None
                        volume_ml = None
                        mode = 'auto'
                        device_id = patient_clear_device.get(pid)
                    # Vitals (only for patients tracking them). SpO2 drifts
                    # 92–99, lower for worse baseline; HR jumps during cough.
                    spo2_pct = None
                    hr_bpm = None
                    if tracks:
                        n_samples = len(wf['samples'])
                        if 'spo2' in tracks:
                            base_spo2 = random.uniform(94.0, 98.5)
                            spo2_samples = [round(max(88.0, min(100.0, base_spo2 + random.uniform(-1.5, 1.0))), 1)
                                            for _ in range(n_samples)]
                            wf['spo2_samples'] = spo2_samples
                            spo2_pct = round(sum(spo2_samples) / n_samples, 1)
                        if 'hr' in tracks:
                            base_hr = random.uniform(70, 92)
                            hr_samples = [round(max(50, min(140, base_hr + random.uniform(-8, 12))), 0)
                                          for _ in range(n_samples)]
                            wf['hr_samples'] = hr_samples
                            hr_bpm = round(sum(hr_samples) / n_samples, 0)
                    session_rows.append((
                        pid, device_id, modality, started_at.isoformat(sep=' ', timespec='seconds'),
                        duration, mode, 1, peak_pressure, peak_flow, volume_ml,
                        spo2_pct, hr_bpm,
                        json.dumps(wf), None,
                    ))

    # Batch insert
    c.executemany("""INSERT INTO therapy_sessions (patient_id, device_id, session_type,
                     started_at, duration_seconds, mode, completed, peak_pressure_cmh2o,
                     peak_flow_lpm, volume_ml, spo2_pct, heart_rate_bpm,
                     waveform_json, notes)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  session_rows)

    # Recompute adherence_pct_30d from actual sessions + update last_session_at
    for pid in all_patient_ids:
        mods = patient_modalities[pid]
        # Sessions in last 30 days per modality
        total_completed = 0
        total_goal = 0
        for modality in mods:
            sessions_30d = c.execute(
                """SELECT COUNT(*) AS n FROM therapy_sessions
                   WHERE patient_id = ? AND session_type = ? AND completed = 1
                     AND started_at > datetime('now', '-30 days')""",
                (pid, modality)).fetchone()['n']
            # Cap at goal × 30 days (can't exceed 100%)
            goal = THERAPY_GOAL_PER_DAY * 30
            total_completed += min(sessions_30d, goal)
            total_goal += goal
        adh_pct = round(100 * total_completed / total_goal) if total_goal else 0
        last_session = c.execute(
            "SELECT MAX(started_at) AS t FROM therapy_sessions WHERE patient_id = ?",
            (pid,)).fetchone()['t']
        c.execute("UPDATE patients SET adherence_pct_30d = ?, last_session_at = ? WHERE id = ?",
                  (adh_pct, last_session, pid))

    print(f'Generated {len(session_rows)} therapy sessions across {len(all_patient_ids)} patients.')

    # ── Clinical history (observations per patient) ────────────────
    priya_id = denver_user_ids.get('priya.s@adapt.com')
    clinical_seed = {
        # mrn: list of (days_ago, admit_days_ago_or_None, admit_reason_or_None,
        #               discharge_days_ago_or_None, spo2, fev1_l, fev1_pp,
        #               fvc_l, fvc_pp, on_o2, o2_flow, o2_type, notes)
        'MRN-448239': [  # Maria Garcia — ALS
            (200, 215, 'Aspiration pneumonia — 5-day admission', 210,
             92, 1.3, 38, 1.9, 42, 1, 1.5, 'nocturnal',
             'Post-admission baseline. Weakness progressing.'),
            (45, None, None, None, 90, 1.1, 32, 1.7, 38, 1, 2.0, 'nocturnal',
             '6-month follow-up. FEV1 continues to decline.'),
        ],
        'MRN-44829': [  # John Smith — COPD
            (80, 95, 'COPD exacerbation', 89,
             88, 1.5, 45, 2.6, 60, 1, 2.0, 'continuous',
             'GOLD stage 3. Post-exacerbation.'),
        ],
        'MRN-51022': [  # Liu Chen — bronchiectasis
            (40, None, None, None, 95, 2.1, 68, 3.2, 78, 0, None, None,
             'Stable on airway clearance.'),
        ],
        'MRN-49203': [  # Sarah Wilson — CF
            (60, None, None, None, 96, 2.4, 72, 3.4, 82, 0, None, None,
             'CF stable. Using 7% HTS nebulizer with Clear.'),
        ],
        'MRN-48990': [  # Robert Davis — late-stage ALS
            (150, 170, 'Respiratory failure, intubated 3 days', 160,
             88, 0.8, 24, 1.2, 28, 1, 3.0, 'continuous',
             'Extubated to NIV + BiWaze Cough.'),
            (30, None, None, None, 89, 0.75, 22, 1.1, 26, 1, 3.0, 'continuous',
             'Continues on continuous O2.'),
        ],
        'MRN-50245': [  # Evelyn Wright
            (100, None, None, None, 91, None, None, None, None, 1, 2.0, 'exertion',
             'O2 during ambulation only.'),
        ],
        'MRN-B-0034': [  # Priscilla Vance — PCD
            (120, None, None, None, 94, 2.0, 65, 3.0, 75, 0, None, None,
             'Primary ciliary dyskinesia. Stable on airway clearance.'),
        ],
        'MRN-B-0051': [  # Miguel Ortiz — early ALS
            (60, None, None, None, 95, 2.6, 78, 3.6, 85, 0, None, None,
             'Early ALS — still ambulatory.'),
        ],
    }
    for mrn, observations in clinical_seed.items():
        pid = (patient_ids_denver.get(mrn) or patient_ids_boulder.get(mrn)
               or patient_ids_sunwest.get(mrn))
        if not pid: continue
        for (days_ago, admit_offset, reason, discharge_offset, spo2, fev1_l,
             fev1_pp, fvc_l, fvc_pp, on_o2, o2_flow, o2_type, notes) in observations:
            obs_date = (date.today() - timedelta(days=days_ago)).isoformat()
            admit = (date.today() - timedelta(days=admit_offset)).isoformat() if admit_offset else None
            disch = (date.today() - timedelta(days=discharge_offset)).isoformat() if discharge_offset else None
            c.execute("""INSERT INTO patient_clinical_history (patient_id, observation_date,
                         hospital_admission_date, hospital_discharge_date, admission_reason,
                         spo2_pct, fev1_liters, fev1_pct_predicted, fvc_liters, fvc_pct_predicted,
                         on_oxygen_therapy, oxygen_flow_lpm, oxygen_type, notes, recorded_by_user_id)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (pid, obs_date, admit, disch, reason, spo2, fev1_l, fev1_pp,
                       fvc_l, fvc_pp, on_o2, o2_flow, o2_type, notes, priya_id))

    # ── Triggered alerts (for Denver demo) ──────────────────────────
    alerts = [
        ('MRN-448239', 'Missed therapy (2 days)', 'critical', 168,
         'No therapy in 3 days',
         'Threshold 2 days exceeded • Rx 3×/day • Last session Apr 14 09:12', 1),
        ('MRN-448239', 'Adherence drop (30d)', 'critical', 0,
         'Supply order overdue (circuit 91d)',
         'Last circuit order 91 days ago; 45-day cycle.', 2),
        ('MRN-44829', 'Missed therapy (2 days)', 'critical', 72,
         'Peak pressure 23% below prescribed',
         'Possible device malfunction; last 14 sessions trending down.', 3),
        ('MRN-51022', 'Adherence drop (30d)', 'warning', 27,
         'Adherence dropped to 62% over past 7 days',
         'Was 89% average over prior 30 days.', 5),
        ('MRN-49203', 'Device offline (48h)', 'warning', 0,
         'Circuit replacement due',
         'Last order 85 days ago; 45-day cycle.', 7),
    ]
    for mrn, rule_name, sev, metric_val, msg, detail, hours_ago in alerts:
        pid = patient_ids_denver.get(mrn)
        rid = rule_ids.get(rule_name)
        if not pid: continue
        triggered = (now - timedelta(hours=hours_ago)).isoformat(sep=' ', timespec='seconds')
        c.execute("""INSERT INTO alerts (organization_id, patient_id, rule_id, triggered_at,
                     severity, metric_value, message, detail)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, pid, rid, triggered, sev, metric_val, msg, detail))

    # ── Demo tasks ──────────────────────────────────────────────────
    james_id = denver_user_ids.get('james.r@adapt.com')
    alex_id  = denver_user_ids.get('alex.k@adapt.com')
    devi_id  = denver_user_ids.get('devi.p@adapt.com')

    # Find seeded alerts so tasks can link to them
    alert_rows = c.execute("""SELECT a.id, a.severity, a.message, p.mrn
                              FROM alerts a JOIN patients p ON p.id = a.patient_id
                              WHERE a.organization_id = ?""", (adap_denver,)).fetchall()
    alerts_by_mrn_msg = {(r['mrn'], r['message']): r['id'] for r in alert_rows}

    garcia_id = patient_ids_denver.get('MRN-448239')
    smith_id  = patient_ids_denver.get('MRN-44829')
    chen_id   = patient_ids_denver.get('MRN-51022')
    patel_id  = patient_ids_denver.get('MRN-50011')
    wilson_id = patient_ids_denver.get('MRN-49203')

    demo_tasks = [
        ('Call Maria Garcia — 72h missed therapy',
         'No Cough sessions in 72 hours. Confirm device function, barriers to use, and set a specific time for next session.',
         'in_progress', 'high', garcia_id,
         ('MRN-448239', 'No therapy in 72 hours'), 2, james_id, priya_id),
        ('Expedite circuit replacement — Maria Garcia',
         'Supply order is 91 days old (45-day cycle). Order replacement circuit and verify delivery date.',
         'pending_external', 'normal', garcia_id,
         ('MRN-448239', 'Supply order overdue (circuit 91d)'), 24, devi_id, priya_id),
        ('Review John Smith pressure anomaly',
         'Peak pressure trending 23% below prescribed for 14 sessions. Investigate device; consider field visit.',
         'todo', 'high', smith_id,
         ('MRN-44829', 'Peak pressure 23% below prescribed'), 4, james_id, priya_id),
        ('Follow up with Liu Chen on adherence drop',
         'Adherence dropped from 89% to 62% over past 7 days. Call to assess barriers.',
         'todo', 'normal', chen_id,
         ('MRN-51022', 'Adherence dropped to 62% over past 7 days'), 48, alex_id, alex_id),
        # Standalone tasks not tied to alerts
        ('Coordinate with Dr. Chen on Rx refill for 3 patients',
         'Three Denver Pulmonary patients approaching 13-month milestone. Batch the refill outreach.',
         'todo', 'normal', None, None, 72, priya_id, priya_id),
        ('Follow up with Medicare on Garcia reauth',
         'Auth expires in 45 days. Initiate reauth paperwork early.',
         'todo', 'low', garcia_id, None, 168, devi_id, priya_id),
        ('Completed — Sarah Wilson circuit reorder',
         'Triggered from supply alert. Circuit ordered, delivered Apr 21.',
         'completed', 'normal', wilson_id, None, -48, devi_id, priya_id),
    ]
    for (title, desc, status, priority, pid, alert_ref, due_hours,
         assignee_id, creator_id) in demo_tasks:
        alert_id_fk = alerts_by_mrn_msg.get(alert_ref) if alert_ref else None
        due_at = ((now + timedelta(hours=due_hours))
                  .isoformat(sep=' ', timespec='seconds') if due_hours is not None else None)
        completed_at = None; completed_by = None
        if status == 'completed':
            completed_at = (now + timedelta(hours=min(due_hours or -24, -1))).isoformat(sep=' ', timespec='seconds')
            completed_by = assignee_id
        c.execute("""INSERT INTO tasks (organization_id, patient_id, alert_id, title, description,
                     status, priority, due_at, assigned_to_user_id, created_by_user_id,
                     completed_at, completed_by_user_id)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, pid, alert_id_fk, title, desc, status, priority,
                   due_at, assignee_id, creator_id, completed_at, completed_by))
        task_id = c.lastrowid
        c.execute("""INSERT INTO task_activity (task_id, user_id, kind, detail)
                     VALUES (?, ?, 'created', ?)""",
                  (task_id, creator_id, f'Task created (priority: {priority})'))
        if status == 'completed':
            c.execute("""INSERT INTO task_activity (task_id, user_id, kind, detail)
                         VALUES (?, ?, 'completed', 'Marked completed')""",
                      (task_id, completed_by))

    # User notification preferences (demo variety)
    c.execute("""UPDATE users SET notify_channel = 'sms', notify_phone_e164 = phone
                 WHERE email IN ('james.r@adapt.com', 'alex.k@adapt.com')""")
    c.execute("""UPDATE users SET notify_channel = 'both' WHERE email = 'priya.s@adapt.com'""")

    # ── Patient engagement: messages, surveys, mood check-ins ─────────
    # Seed a realistic mix so the Inbox, mood trend, and survey sections
    # all have content to demo.

    # Convenience: pick a handful of Denver patients for engagement seeding.
    _denver_mrns = ['MRN-448239', 'MRN-44829', 'MRN-51022', 'MRN-47885',
                    'MRN-48990', 'MRN-44118', 'MRN-50245', 'MRN-50401',
                    'MRN-50512', 'MRN-50188']
    _denver_pts = [(m, patient_ids_denver[m]) for m in _denver_mrns
                   if m in patient_ids_denver]

    # Messages: a mix of inbound (from_patient) and replies (from_provider).
    message_seed = [
        # (mrn, hours_ago, direction, author_user_id_or_none, body)
        ('MRN-448239', 8,   'from_patient',  None,       "Mask seal has been leaking all week. Any tips?"),
        ('MRN-448239', 5,   'from_provider', priya_id,   "Sorry to hear! Swap to the medium cushion in your kit. I'll call you tomorrow to check in."),
        ('MRN-44829',  20,  'from_patient',  None,       "Can you confirm my next supply shipment date?"),
        ('MRN-51022',  36,  'from_patient',  None,       "I've been skipping evening therapy because I'm too tired. Is that ok?"),
        ('MRN-47885',  60,  'from_patient',  None,       "Device showed an error code E-03 last night."),
        ('MRN-47885',  55,  'from_provider', james_id,   "Received — cycling the device usually clears E-03. Please try that and let me know."),
        ('MRN-50401',  100, 'from_patient',  None,       "Thanks for the follow-up call yesterday — really helped."),
    ]
    msg_ids_by_mrn = {}  # first message in thread for that patient
    for mrn, hrs_ago, direction, author_id, body in message_seed:
        if mrn not in patient_ids_denver: continue
        pid = patient_ids_denver[mrn]
        ts = (now - timedelta(hours=hrs_ago)).isoformat(sep=' ', timespec='seconds')
        thread_id = msg_ids_by_mrn.get(mrn)  # None for first in thread
        c.execute("""INSERT INTO patient_messages (organization_id, patient_id, thread_id,
                     direction, author_user_id, body, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, pid, thread_id, direction, author_id, body, ts))
        mid = c.lastrowid
        # Back-fill the thread root for the first message of each thread
        if thread_id is None:
            c.execute("UPDATE patient_messages SET thread_id = ? WHERE id = ?", (mid, mid))
            msg_ids_by_mrn[mrn] = mid
            # Inbox: every inbound message creates an entry. Provider replies don't.
            if direction == 'from_patient':
                assignee = priya_id  # default to location admin; reassignable in UI
                c.execute("""INSERT INTO inbox_items (organization_id, patient_id, kind,
                             ref_id, status, assigned_to_user_id, created_at)
                             VALUES (?, ?, 'message', ?, 'unread', ?, ?)""",
                          (adap_denver, pid, mid, assignee, ts))
                ii_id = c.lastrowid
                # Auto-create a "respond to patient" task linked to this inbox item.
                # Task is the canonical assigned-work record; the inbox is just the UI.
                pname = c.execute("SELECT first_name FROM patients WHERE id = ?", (pid,)).fetchone()[0]
                task_title = f"Respond to {pname}"
                due_hours = 24
                due_at = (now + timedelta(hours=due_hours) - timedelta(hours=hrs_ago)).isoformat(sep=' ', timespec='seconds')
                c.execute("""INSERT INTO tasks (organization_id, patient_id, inbox_item_id,
                             title, description, status, priority, due_at,
                             assigned_to_user_id, created_by_user_id, created_at)
                             VALUES (?, ?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?)""",
                          (adap_denver, pid, ii_id, task_title, body[:200],
                           'high' if hrs_ago > 24 else 'normal',
                           due_at, assignee, assignee, ts))
        elif direction == 'from_patient':
            # New inbound reply on an existing thread
            assignee = priya_id
            c.execute("""INSERT INTO inbox_items (organization_id, patient_id, kind,
                         ref_id, status, assigned_to_user_id, created_at)
                         VALUES (?, ?, 'message', ?, 'unread', ?, ?)""",
                      (adap_denver, pid, mid, assignee, ts))
            ii_id = c.lastrowid
            pname = c.execute("SELECT first_name FROM patients WHERE id = ?", (pid,)).fetchone()[0]
            due_hours = 24
            due_at = (now + timedelta(hours=due_hours) - timedelta(hours=hrs_ago)).isoformat(sep=' ', timespec='seconds')
            c.execute("""INSERT INTO tasks (organization_id, patient_id, inbox_item_id,
                         title, description, status, priority, due_at,
                         assigned_to_user_id, created_by_user_id, created_at)
                         VALUES (?, ?, ?, ?, ?, 'todo', 'normal', ?, ?, ?, ?)""",
                      (adap_denver, pid, ii_id, f"Respond to {pname}", body[:200],
                       due_at, assignee, assignee, ts))

    # Surveys — 30/60/90-day. Not every patient has hit each milestone.
    # Score formula: round(avg(q1..q5) * 20).
    survey_seed = [
        # (mrn, milestone, q1, q2, q3, q4, q5, open_text, days_ago)
        ('MRN-448239', 30, 4, 4, 3, 4, 5, "Device is easy to use. Sometimes the mask is uncomfortable by the end of the session.", 85),
        ('MRN-448239', 60, 5, 4, 4, 4, 5, "Breathing is noticeably better day-to-day.", 55),
        ('MRN-448239', 90, 5, 5, 4, 5, 5, "Very happy with therapy.", 25),
        ('MRN-44829',  30, 3, 3, 3, 3, 4, "Schedule is tight with work, doing my best.", 60),
        ('MRN-44829',  60, 4, 3, 4, 4, 4, "Learning to fit it in around meetings.", 30),
        ('MRN-51022',  30, 2, 2, 3, 3, 3, "Having trouble with evening sessions — too tired.", 45),
        ('MRN-47885',  30, 4, 4, 4, 3, 5, "All good.", 70),
        ('MRN-47885',  60, 4, 4, 4, 3, 5, "Steady progress.", 40),
        ('MRN-48990',  30, 5, 5, 5, 5, 5, "Everything going great, no issues.", 40),
        ('MRN-50188',  30, 3, 3, 4, 4, 4, "Mostly good, would love a better portable case.", 18),
    ]
    for mrn, milestone, q1, q2, q3, q4, q5, open_txt, days_ago in survey_seed:
        if mrn not in patient_ids_denver: continue
        pid = patient_ids_denver[mrn]
        pname = c.execute("SELECT first_name FROM patients WHERE id = ?", (pid,)).fetchone()[0]
        ts_completed = (now - timedelta(days=days_ago)).isoformat(sep=' ', timespec='seconds')
        ts_sent = (now - timedelta(days=days_ago + 2)).isoformat(sep=' ', timespec='seconds')
        score = round(((q1 + q2 + q3 + q4 + q5) / 5) * 20)
        c.execute("""INSERT INTO patient_surveys (organization_id, patient_id, milestone,
                     q1_confidence, q2_manageable, q3_breathing_better, q4_tolerance,
                     q5_connected, q6_open_response, score_0_100, sent_at, completed_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, pid, milestone, q1, q2, q3, q4, q5, open_txt,
                   score, ts_sent, ts_completed))
        sid = c.lastrowid
        c.execute("""INSERT INTO inbox_items (organization_id, patient_id, kind,
                     ref_id, status, assigned_to_user_id, created_at)
                     VALUES (?, ?, 'survey', ?, 'unread', ?, ?)""",
                  (adap_denver, pid, sid, priya_id, ts_completed))
        ii_id = c.lastrowid
        # Tasks mirror inbox items so the work queue is a single source of truth.
        c.execute("""INSERT INTO tasks (organization_id, patient_id, inbox_item_id,
                     title, description, status, priority, due_at,
                     assigned_to_user_id, created_by_user_id, created_at)
                     VALUES (?, ?, ?, ?, ?, 'todo', 'low', NULL, ?, ?, ?)""",
                  (adap_denver, pid, ii_id,
                   f"Review {milestone}-day survey — {pname}",
                   open_txt or f"Score {score}", priya_id, priya_id, ts_completed))

    # Daily mood check-ins — 30 days per seeded patient with a realistic pattern.
    # The mood distribution follows each patient's adherence tier so demos look
    # coherent (healthy adherence → mostly happy; poor adherence → more sad/meh).
    _mood_rng = random.Random(42)
    def _tier_moods(adh):
        """Return a weighted mood based on the patient's 30-day adherence."""
        adh = adh or 0
        if adh >= 80:
            return _mood_rng.choices(['happy','ok','meh','sad'], [0.55, 0.30, 0.10, 0.05])[0]
        if adh >= 50:
            return _mood_rng.choices(['happy','ok','meh','sad'], [0.25, 0.35, 0.25, 0.15])[0]
        return _mood_rng.choices(['happy','ok','meh','sad'], [0.10, 0.25, 0.35, 0.30])[0]

    _sad_notes = [
        "Feeling really short of breath today.",
        "Didn't sleep well, coughing overnight.",
        "Chest feels tight this morning.",
        "Exhausted — might skip today's session.",
    ]
    _meh_notes = [
        "Mask irritated my face today.",
        "A little more tired than usual.",
        "Throat felt dry during therapy.",
    ]

    # Pull adherence once per patient
    adh_by_pid = {}
    for _mrn, _pid in _denver_pts:
        _row = c.execute("SELECT adherence_pct_30d FROM patients WHERE id = ?", (_pid,)).fetchone()
        adh_by_pid[_pid] = (_row[0] if _row else 0) or 0

    for mrn, pid in _denver_pts:
        adh = adh_by_pid.get(pid, 0)
        for d in range(30, 0, -1):
            # ~85% chance they log a mood on any given day
            if _mood_rng.random() > 0.85: continue
            mood = _tier_moods(adh)
            note = None
            # Only sad/meh can carry a response and only ~35% of them do
            if mood == 'sad' and _mood_rng.random() < 0.5:
                note = _mood_rng.choice(_sad_notes)
            elif mood == 'meh' and _mood_rng.random() < 0.35:
                note = _mood_rng.choice(_meh_notes)
            ts = (now - timedelta(days=d, hours=_mood_rng.randint(6, 21))).isoformat(sep=' ', timespec='seconds')
            c.execute("""INSERT INTO patient_moods (organization_id, patient_id, mood,
                         response_text, recorded_at)
                         VALUES (?, ?, ?, ?, ?)""",
                      (adap_denver, pid, mood, note, ts))
            mid = c.lastrowid
            # Unfavorable mood + note → inbox entry + linked task
            if note and mood in ('sad', 'meh'):
                c.execute("""INSERT INTO inbox_items (organization_id, patient_id, kind,
                             ref_id, status, assigned_to_user_id, created_at)
                             VALUES (?, ?, 'mood', ?, 'unread', ?, ?)""",
                          (adap_denver, pid, mid, priya_id, ts))
                ii_id = c.lastrowid
                pname = c.execute("SELECT first_name FROM patients WHERE id = ?", (pid,)).fetchone()[0]
                due_at = (now - timedelta(days=d - 1)).isoformat(sep=' ', timespec='seconds')
                c.execute("""INSERT INTO tasks (organization_id, patient_id, inbox_item_id,
                             title, description, status, priority, due_at,
                             assigned_to_user_id, created_by_user_id, created_at)
                             VALUES (?, ?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?)""",
                          (adap_denver, pid, ii_id, f"Check in with {pname}", note,
                           'normal' if mood == 'meh' else 'high',
                           due_at, priya_id, priya_id, ts))

    # ── Default assignees for inbound engagement items ──────────────
    # Location admin gets any unassigned messages/mood-notes by default.
    c.execute("UPDATE organizations SET default_assignee_user_id = ? WHERE id = ?",
              (priya_id, adap_denver))
    linda_id = c.execute("SELECT id FROM users WHERE email = 'linda.w@adapt.com'").fetchone()[0]
    c.execute("UPDATE organizations SET default_assignee_user_id = ? WHERE id = ?",
              (linda_id, adap_boulder))

    # ── Mobile app paired/not-paired ─────────────────────────────────
    # Most active patients have paired their mobile app; a handful haven't
    # (so the UI can show the "not paired — engagement features inactive"
    # state). Deterministic via patient id parity.
    c.execute("UPDATE patients SET mobile_app_enabled = 1 WHERE status = 'active'")
    c.execute("UPDATE patients SET mobile_app_enabled = 0 WHERE id IN (?, ?)",
              (patient_ids_denver.get('MRN-50188'),
               patient_ids_denver.get('MRN-50401')))

    # Realistic mix of last-sync timestamps: most recently synced within
    # the last 24 hours; a couple stale (2–5 days) to demo the warning state.
    _sync_rng = random.Random(3141)
    _paired_rows = c.execute("SELECT id FROM patients WHERE mobile_app_enabled = 1").fetchall()
    for (pid,) in _paired_rows:
        # Most patients synced in the last 24h; a handful 2–5 days ago.
        if _sync_rng.random() < 0.85:
            hours_ago = _sync_rng.randint(1, 36)
        else:
            hours_ago = _sync_rng.randint(48, 120)
        ts = (now - timedelta(hours=hours_ago)).isoformat(sep=' ', timespec='seconds')
        c.execute("UPDATE patients SET mobile_app_last_sync = ? WHERE id = ?",
                  (ts, pid))

    # ── Patient referral history ─────────────────────────────────────
    # For every patient that currently has a referring clinic, record an
    # open history row (assigned_at = patient creation date, removed_at = NULL).
    # Gives the clinic-detail page and patient detail something to show.
    c.execute("""INSERT INTO patient_referral_history
                 (organization_id, patient_id, clinic_id, provider_id,
                  assigned_at, changed_by_user_id, reason)
                 SELECT p.organization_id, p.id, p.referring_clinic_id,
                        p.referring_provider_id, p.created_at, ?, 'Initial assignment'
                 FROM patients p
                 WHERE p.referring_clinic_id IS NOT NULL""", (priya_id,))

    # One demo patient had a previous (removed) referral — to show history
    # for a patient who switched clinics.
    maria_pid = patient_ids_denver.get('MRN-448239')
    if maria_pid:
        _prev_clinic = c.execute("""SELECT id FROM referring_clinics
                                    WHERE organization_id = ? AND name != (
                                      SELECT rc.name FROM referring_clinics rc
                                      WHERE rc.id = (SELECT referring_clinic_id
                                                     FROM patients WHERE id = ?))
                                    LIMIT 1""", (adap_denver, maria_pid)).fetchone()
        if _prev_clinic:
            past = (now - timedelta(days=420)).isoformat(sep=' ', timespec='seconds')
            removed = (now - timedelta(days=300)).isoformat(sep=' ', timespec='seconds')
            c.execute("""INSERT INTO patient_referral_history
                         (organization_id, patient_id, clinic_id, provider_id,
                          assigned_at, removed_at, changed_by_user_id, reason)
                         VALUES (?, ?, ?, NULL, ?, ?, ?, 'Patient switched practices')""",
                      (adap_denver, maria_pid, _prev_clinic[0], past, removed, priya_id))

    # ── Survey opt-out demo ──────────────────────────────────────────
    # One patient declined the 60-day survey so the UI can show the
    # opted-out state alongside completed / not-completed tiles.
    opt_out_pid = patient_ids_denver.get('MRN-44829')
    if opt_out_pid:
        opted_at = (now - timedelta(days=18)).isoformat(sep=' ', timespec='seconds')
        c.execute("""INSERT OR REPLACE INTO patient_surveys
                     (organization_id, patient_id, milestone, sent_at,
                      opted_out, opted_out_at, opted_out_reason)
                     VALUES (?, ?, 60, ?, 1, ?, 'Patient declined via app')""",
                  (adap_denver, opt_out_pid, opted_at, opted_at))

    # ── Access log seed (a handful of prior events) ──────────────────
    # Demo data so the audit page isn't empty on first open.
    _access_seed = [
        (1,   'patient_view',     priya_id, patient_ids_denver.get('MRN-448239'), 'patient', None),
        (2,   'patient_view',     james_id, patient_ids_denver.get('MRN-448239'), 'patient', None),
        (4,   'session_view',     priya_id, patient_ids_denver.get('MRN-44829'),  'session', None),
        (6,   'report_generate',  priya_id, patient_ids_denver.get('MRN-448239'), 'report',  '30-day therapy summary'),
        (20,  'patient_view',     alex_id,  patient_ids_denver.get('MRN-51022'),  'patient', None),
        (48,  'patient_view',     priya_id, patient_ids_denver.get('MRN-47885'),  'patient', None),
    ]
    for hrs_ago, ev, uid, pid, ref_type, detail in _access_seed:
        if pid is None: continue
        occurred = (now - timedelta(hours=hrs_ago)).isoformat(sep=' ', timespec='seconds')
        c.execute("""INSERT INTO access_log
                     (organization_id, user_id, event, patient_id, ref_type, detail, occurred_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (adap_denver, uid, ev, pid, ref_type, detail, occurred))

    # ══════════════════════════════════════════════════════════════
    # Patient goals (what the patient tracks in the mobile app)
    # ══════════════════════════════════════════════════════════════
    # Each patient gets one goal per prescribed BiWaze modality at
    # THERAPY_GOAL_PER_DAY sessions/day. About half also track vest,
    # breathing treatments, steps, and/or sleep.
    today = date.today()
    goal_start = today - timedelta(days=44)
    for pid in all_patient_ids:
        mods = c.execute("""SELECT DISTINCT modality FROM patient_prescriptions
                            WHERE patient_id = ? AND is_active = 1""", (pid,)).fetchall()
        patient_goal_types = []
        for m in mods:
            gt = 'therapy_cough' if m['modality'] == 'cough' else 'therapy_clear'
            patient_goal_types.append((gt, float(THERAPY_GOAL_PER_DAY), 'sessions'))
        # Optional goals — roll dice per patient
        if random.random() < 0.35:
            patient_goal_types.append(('vest', float(random.choice([2, 3])), 'sessions'))
        if random.random() < 0.45:
            patient_goal_types.append(('breathing_treatment', float(random.choice([2, 3, 4])), 'sessions'))
        if random.random() < 0.55:
            patient_goal_types.append(('steps', float(random.choice([3000, 4000, 5000, 6000])), 'steps'))
        if random.random() < 0.50:
            patient_goal_types.append(('sleep', float(random.choice([7, 7.5, 8])), 'hours'))

        for gt, target, unit in patient_goal_types:
            c.execute("""INSERT INTO patient_goals
                         (patient_id, goal_type, target_value, unit, start_date, end_date)
                         VALUES (?,?,?,?,?,NULL)""",
                      (pid, gt, target, unit, goal_start.isoformat()))
            goal_id = c.lastrowid
            # Generate daily log rows. For therapy_* we mirror the seeded
            # therapy_sessions count per day so the Goals card matches the
            # Therapy data card. Others are plausible mobile-app numbers.
            logs = []
            for d in range(45):
                day = goal_start + timedelta(days=d)
                if gt == 'therapy_cough':
                    n = c.execute("""SELECT COUNT(*) AS n FROM therapy_sessions
                                     WHERE patient_id = ? AND session_type='cough'
                                       AND date(started_at)=date(?)""",
                                  (pid, day.isoformat())).fetchone()['n']
                    actual = float(n)
                elif gt == 'therapy_clear':
                    n = c.execute("""SELECT COUNT(*) AS n FROM therapy_sessions
                                     WHERE patient_id = ? AND session_type='clear'
                                       AND date(started_at)=date(?)""",
                                  (pid, day.isoformat())).fetchone()['n']
                    actual = float(n)
                elif gt == 'vest':
                    actual = float(max(0, round(target - random.choice([0, 0, 0, 1, 1, 2]) + random.uniform(-0.3, 0.3))))
                elif gt == 'breathing_treatment':
                    actual = float(max(0, round(target - random.choice([0, 0, 0, 1, 1, 2]))))
                elif gt == 'steps':
                    actual = float(max(0, int(target * random.uniform(0.4, 1.25))))
                else:  # sleep
                    actual = round(max(0.0, target + random.uniform(-2.2, 0.8)), 1)
                logs.append((goal_id, day.isoformat(), actual))
            c.executemany("""INSERT INTO patient_goal_log (goal_id, log_date, actual_value)
                             VALUES (?,?,?)""", logs)

    conn.commit()
    conn.close()
    print(f'Seeded {DB_PATH}')
    print('Users available at login screen:')
    print('  support@abmrespiratory.com (ABMRC super admin — view-only across all orgs)')
    print('  karen.h@adapt.com     (Adapt HQ admin — parent rollup + switch location)')
    print('  priya.s@adapt.com     (Adapt — Denver admin — full location admin)')
    print('  james.r@adapt.com     (Adapt — Denver clinician)')
    print('  linda.w@adapt.com     (Adapt — Boulder admin)')
    print('  maria.t@sunwest.com  (Sunwest Medical admin)')


if __name__ == '__main__':
    seed()
