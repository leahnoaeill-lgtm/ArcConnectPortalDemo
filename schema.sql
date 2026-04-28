-- Arc Connect Provider Portal schema
-- SQLite

PRAGMA foreign_keys = ON;

-- Organizations (parent/child hierarchy)
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    type TEXT NOT NULL CHECK(type IN ('parent', 'location', 'internal')),
    -- Lifecycle status. 'pending_setup' = BAA uploaded + parent admin invited
    -- but invite not yet accepted; 'active' = normal operating state;
    -- 'suspended' = BAA expired or revoked, super admin paused access.
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('pending_setup', 'active', 'suspended')),
    logo_path TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    phone TEXT,
    email TEXT,
    npi TEXT,
    timezone TEXT DEFAULT 'America/New_York',
    latitude REAL,
    longitude REAL,
    -- Patient-engagement feature toggles. Parent "off" overrides children
    -- (mood check-in itself is always on; only the free-text response is gated).
    messaging_enabled INTEGER DEFAULT 1,
    mood_response_enabled INTEGER DEFAULT 1,
    -- Who manages alert rules: each location individually, or the parent org
    -- for every child location. Only meaningful on parent rows; children read
    -- this value from their parent. Default = 'location' (per-location).
    alert_rules_source TEXT DEFAULT 'location' CHECK(alert_rules_source IN ('location','parent')),
    -- Default assignee for inbound patient-engagement items (messages, mood
    -- notes). Configurable per location under Settings → Location info.
    default_assignee_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- Super-admin verification of the customer (NPI confirmed, contract on
    -- file, etc.). Captured on the New Organization form; can be edited later.
    verification_complete INTEGER DEFAULT 0,
    verification_date TEXT,
    verification_notes TEXT,
    verified_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_org_parent ON organizations(parent_id);

-- Users
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email TEXT UNIQUE NOT NULL,
    first_name TEXT,
    last_name TEXT,
    role TEXT NOT NULL CHECK(role IN ('admin', 'clinician', 'billing', 'read_only', 'customer_service', 'account_executive', 'super_admin')),
    phone TEXT,
    is_active INTEGER DEFAULT 1,
    last_login_at TIMESTAMP,
    rss_feeds_json TEXT,  -- JSON array of subscribed feed keys; NULL = use defaults
    notify_channel TEXT DEFAULT 'email',  -- 'email','sms','both','none'
    notify_phone_e164 TEXT,  -- E.164 SMS target; falls back to 'phone' if NULL
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_org ON users(organization_id);

-- Tasks (assigned follow-up work, optionally spawned from an alert)
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    patient_id INTEGER REFERENCES patients(id) ON DELETE SET NULL,
    alert_id INTEGER REFERENCES alerts(id) ON DELETE SET NULL,
    -- Link to an inbox item (message / mood-note / survey) that this task
    -- represents. Nullable: most tasks are standalone. See inbox_items below.
    inbox_item_id INTEGER REFERENCES inbox_items(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'todo' CHECK(status IN ('todo','in_progress','pending_external','completed','cancelled')),
    priority TEXT DEFAULT 'normal' CHECK(priority IN ('high','normal','low')),
    due_at TIMESTAMP,
    assigned_to_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    completed_at TIMESTAMP,
    completed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_org ON tasks(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assigned_to_user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_alert ON tasks(alert_id);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_inbox ON tasks(inbox_item_id);

-- Task activity log (who did what)
CREATE TABLE IF NOT EXISTS task_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,  -- 'created','assigned','status_changed','comment','completed','reassigned'
    detail TEXT,
    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_activity_task ON task_activity(task_id, occurred_at);

-- Referring clinics (external MD practices that refer patients)
CREATE TABLE IF NOT EXISTS referring_clinics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    npi TEXT,
    phone TEXT,
    email TEXT,
    website_url TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    notes TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ref_clinics_org ON referring_clinics(organization_id);

-- Referring providers (external MDs/NPs/PAs within a referring clinic)
CREATE TABLE IF NOT EXISTS referring_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    clinic_id INTEGER NOT NULL REFERENCES referring_clinics(id) ON DELETE CASCADE,
    first_name TEXT,
    last_name TEXT,
    credentials TEXT,
    specialty TEXT,
    npi TEXT,
    phone TEXT,
    email TEXT,
    notes TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ref_providers_org ON referring_providers(organization_id);
CREATE INDEX IF NOT EXISTS idx_ref_providers_clinic ON referring_providers(clinic_id);

-- Patients
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    mrn TEXT,
    first_name TEXT,
    last_name TEXT,
    dob DATE,
    phone TEXT,
    email TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    preferred_language TEXT DEFAULT 'en-US',
    rx_frequency_per_day INTEGER,
    rx_modalities TEXT,  -- JSON
    capped_rental_start DATE,
    capped_rental_end DATE,
    assigned_clinician_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    referring_clinic_id INTEGER REFERENCES referring_clinics(id) ON DELETE SET NULL,
    referring_provider_id INTEGER REFERENCES referring_providers(id) ON DELETE SET NULL,
    diagnosis TEXT,
    adherence_pct_30d INTEGER,
    last_session_at TIMESTAMP,
    status TEXT DEFAULT 'active' CHECK(status IN ('active','inactive')),
    notes TEXT,
    -- Whether the patient has paired the Arc Connect mobile app. When 0,
    -- messaging / surveys / mood check-ins have no delivery channel and the
    -- portal hides their "send" actions. Read-only display field in the UI;
    -- flipped by the mobile app at pairing time (or by admin in a fix-up).
    mobile_app_enabled INTEGER DEFAULT 0,
    -- Most recent successful sync with the mobile app (heartbeat / data push).
    -- Updated by the mobile app on each sync; displayed on the patient header.
    mobile_app_last_sync TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Patient clinical history (observations over time)
CREATE TABLE IF NOT EXISTS patient_clinical_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    observation_date DATE NOT NULL,
    -- Hospital admission
    hospital_admission_date DATE,
    hospital_discharge_date DATE,
    admission_reason TEXT,
    -- Vitals / pulmonary function
    spo2_pct INTEGER,                -- 0–100
    fev1_liters REAL,
    fev1_pct_predicted INTEGER,
    fvc_liters REAL,
    fvc_pct_predicted INTEGER,
    -- Oxygen therapy status at this observation
    on_oxygen_therapy INTEGER DEFAULT 0,
    oxygen_flow_lpm REAL,
    oxygen_type TEXT CHECK(oxygen_type IN ('continuous','nocturnal','prn','exertion') OR oxygen_type IS NULL),
    notes TEXT,
    recorded_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hx_patient ON patient_clinical_history(patient_id, observation_date DESC);

CREATE INDEX IF NOT EXISTS idx_patients_org ON patients(organization_id);

-- Patient insurance (one active row per coverage_type per patient)
CREATE TABLE IF NOT EXISTS patient_insurance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    payer_name TEXT NOT NULL,
    plan_type TEXT,
    member_id TEXT,
    group_number TEXT,
    coverage_type TEXT DEFAULT 'primary' CHECK(coverage_type IN ('primary','secondary','tertiary')),
    effective_date DATE,
    -- Payers approve each BiWaze device separately, so a patient with both
    -- Cough and Clear has two approval dates + two authorization numbers.
    cough_approval_date DATE,
    cough_auth_number TEXT,
    clear_approval_date DATE,
    clear_auth_number TEXT,
    -- Legacy columns kept for now so older rows still render. Populated on
    -- save only when the patient has exactly one modality (falls through to
    -- the per-modality column). New forms write only the per-modality fields.
    approval_date DATE,
    auth_number TEXT,
    next_review_date DATE,
    notes TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_insurance_patient ON patient_insurance(patient_id);

-- Patient prescriptions (one active row per patient per modality; history retained)
CREATE TABLE IF NOT EXISTS patient_prescriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    modality TEXT NOT NULL CHECK(modality IN ('cough','clear')),
    prescribed_by_provider_id INTEGER REFERENCES referring_providers(id) ON DELETE SET NULL,
    prescribed_date DATE,
    effective_date DATE,

    -- Cough settings
    cough_cycles INTEGER,
    cough_insp_pressure_cmh2o REAL,
    cough_insp_time_sec REAL,
    cough_exp_pressure_cmh2o REAL,
    cough_exp_time_sec REAL,
    cough_pause_pressure_cmh2o REAL,
    cough_pause_time_sec REAL,

    -- Clear settings
    clear_pep_pressure_cmh2o REAL,
    clear_pep_time_sec REAL,
    clear_osc_pressure_cmh2o REAL,
    clear_osc_time_sec REAL,
    clear_neb_enabled INTEGER,
    clear_neb_medication TEXT,
    clear_bleed_in_oxygen INTEGER DEFAULT 0,
    clear_oxygen_flow_lpm REAL,

    -- Optional uploaded Rx document (the paper/PDF prescription from the
    -- referring MD). Stored under static/uploads/prescriptions/.
    document_path TEXT,
    document_filename TEXT,

    notes TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rx_patient ON patient_prescriptions(patient_id, modality, is_active);

-- Therapy sessions (one row per completed or attempted therapy)
CREATE TABLE IF NOT EXISTS therapy_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    session_type TEXT NOT NULL CHECK(session_type IN ('cough','clear')),
    started_at TIMESTAMP NOT NULL,
    duration_seconds INTEGER,
    mode TEXT,
    completed INTEGER DEFAULT 1,
    peak_pressure_cmh2o REAL,  -- peak inspiratory pressure for Cough sessions
    peak_flow_lpm REAL,        -- Peak Cough Flow (PCF) for Cough sessions
    volume_ml REAL,            -- Insufflation volume per session (Cough only)
    -- Optional per-session vitals, captured when the patient uses a paired
    -- pulse-ox / wearable alongside their BiWaze device. NULL when the
    -- patient isn't tracking that signal.
    spo2_pct REAL,             -- Average SpO2 during session (%)
    heart_rate_bpm REAL,       -- Average heart rate during session (bpm)
    waveform_json TEXT,  -- JSON: {"sample_rate_hz": 2, "samples": [...], "spo2_samples": [...]?, "hr_samples": [...]?}
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sessions_patient ON therapy_sessions(patient_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_type ON therapy_sessions(patient_id, session_type, started_at DESC);

-- Devices
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    serial_number TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL CHECK(model IN ('biwaze_cough', 'biwaze_clear')),
    firmware_version TEXT,
    upload_date DATE,
    last_communication TIMESTAMP,
    warranty_end DATE,
    status TEXT DEFAULT 'in_stock' CHECK(status IN ('in_stock','in_use','maintenance','retired')),
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_devices_org ON devices(organization_id);

-- Device assignments (patient ↔ device, time-bounded)
CREATE TABLE IF NOT EXISTS device_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    assigned_date DATE NOT NULL,
    returned_date DATE,
    consent_form_path TEXT NOT NULL,
    consent_form_original_name TEXT,
    assigned_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_assign_patient ON device_assignments(patient_id);
CREATE INDEX IF NOT EXISTS idx_assign_device ON device_assignments(device_id);

-- Alert rules (per org)
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    metric TEXT NOT NULL,
    threshold_value REAL NOT NULL,
    window_hours INTEGER,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    notify_email INTEGER DEFAULT 1,
    notify_in_app INTEGER DEFAULT 1,
    notify_sms INTEGER DEFAULT 0,
    notify_recipient_roles TEXT,  -- JSON: ["admin","clinician"]
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rules_org ON alert_rules(organization_id);

-- Alerts (triggered)
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    rule_id INTEGER REFERENCES alert_rules(id) ON DELETE SET NULL,
    triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
    metric_value REAL,
    message TEXT,
    detail TEXT,
    acknowledged_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_at TIMESTAMP,
    resolved_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alerts_org ON alerts(organization_id);
CREATE INDEX IF NOT EXISTS idx_alerts_patient ON alerts(patient_id);


-- ═══════════════════════════════════════════════════════════════════
-- Patient engagement: messaging, surveys, mood check-ins
-- ═══════════════════════════════════════════════════════════════════

-- Individual messages (two-way thread). Direction distinguishes
-- patient-originated from provider replies.
CREATE TABLE IF NOT EXISTS patient_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    thread_id INTEGER,              -- points to the first message in the thread
    direction TEXT NOT NULL CHECK(direction IN ('from_patient','from_provider')),
    author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- null when from patient
    body TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_msgs_patient ON patient_messages(patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_msgs_thread  ON patient_messages(thread_id);

-- Daily mood check-ins. Mood itself is always captured; the free-text
-- response is only allowed when the org's mood_response_enabled flag is on.
CREATE TABLE IF NOT EXISTS patient_moods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    mood TEXT NOT NULL CHECK(mood IN ('sad','meh','ok','happy')),
    response_text TEXT,             -- optional, only on unfavorable moods
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_moods_patient ON patient_moods(patient_id, recorded_at DESC);

-- 30/60/90-day survey responses. Six-question instrument (same at each
-- milestone) so scores trend over time.
CREATE TABLE IF NOT EXISTS patient_surveys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    milestone INTEGER NOT NULL CHECK(milestone IN (30,60,90)),
    q1_confidence       INTEGER,    -- 1-5
    q2_manageable       INTEGER,    -- 1-5
    q3_breathing_better INTEGER,    -- 1-5
    q4_tolerance        INTEGER,    -- 1-5
    q5_connected        INTEGER,    -- 1-5
    q6_open_response    TEXT,
    score_0_100 INTEGER,            -- computed aggregate (avg of q1-q5 × 20)
    sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    -- Patients can decline future surveys. When opted_out is set, the row
    -- represents the patient's explicit decline for this milestone so the
    -- portal shows "Opted out" instead of "Not completed."
    opted_out       INTEGER DEFAULT 0,
    opted_out_at    TIMESTAMP,
    opted_out_reason TEXT,
    UNIQUE(patient_id, milestone)
);
CREATE INDEX IF NOT EXISTS idx_surveys_patient ON patient_surveys(patient_id, milestone);

-- Unified inbox queue. Each row points back to one patient_messages,
-- patient_surveys, or patient_moods row via (kind, ref_id). A mood only
-- creates an inbox item when it has a non-empty response_text AND mood is
-- 'sad' or 'meh'. Every inbound message creates one; every completed
-- survey creates one.
CREATE TABLE IF NOT EXISTS inbox_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('message','survey','mood')),
    ref_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unread' CHECK(status IN ('unread','read','resolved')),
    assigned_to_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at  TIMESTAMP,
    resolved_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_org ON inbox_items(organization_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_patient ON inbox_items(patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_assignee ON inbox_items(assigned_to_user_id);


-- ═══════════════════════════════════════════════════════════════════
-- Referral history (append-only log of clinic/provider reassignments)
-- ═══════════════════════════════════════════════════════════════════
-- Captures every change to a patient's referring clinic/provider so we
-- can show historic assignments with dates. patients.referring_clinic_id
-- and .referring_provider_id remain the current snapshot; this table is
-- the time-series behind them.

CREATE TABLE IF NOT EXISTS patient_referral_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    -- Nullable so we can track a patient who has a clinic but no provider
    -- or vice versa. References are SET NULL so deleting a clinic doesn't
    -- destroy the historic record.
    clinic_id   INTEGER REFERENCES referring_clinics(id) ON DELETE SET NULL,
    provider_id INTEGER REFERENCES referring_providers(id) ON DELETE SET NULL,
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    removed_at  TIMESTAMP,                -- NULL = still active
    changed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_refhist_patient ON patient_referral_history(patient_id, assigned_at DESC);
CREATE INDEX IF NOT EXISTS idx_refhist_clinic ON patient_referral_history(clinic_id);


-- ═══════════════════════════════════════════════════════════════════
-- Patient goals (what the patient is tracking in the mobile app)
-- ═══════════════════════════════════════════════════════════════════
-- One row per active goal. BiWaze therapy goals track sessions/day for
-- the prescribed modality (target from prescription). Vest + breathing
-- treatment goals also track sessions/day. Steps + sleep are free-form
-- daily targets.
--
-- goal_type:
--   'therapy_cough'      — BiWaze Cough sessions / day
--   'therapy_clear'      — BiWaze Clear sessions / day
--   'vest'               — Vest therapy sessions / day
--   'breathing_treatment'— Nebulizer / breathing treatments / day
--   'steps'              — Daily step target
--   'sleep'              — Daily sleep hours target
--
-- unit:
--   'sessions' (therapy_cough, therapy_clear, vest, breathing_treatment)
--   'steps'    (steps)
--   'hours'    (sleep)

CREATE TABLE IF NOT EXISTS patient_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    goal_type TEXT NOT NULL CHECK(goal_type IN (
        'therapy_cough','therapy_clear','vest','breathing_treatment','steps','sleep'
    )),
    target_value REAL NOT NULL,
    unit TEXT NOT NULL CHECK(unit IN ('sessions','steps','hours')),
    start_date DATE NOT NULL,
    end_date DATE,    -- NULL = still active
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_goals_patient ON patient_goals(patient_id, goal_type, end_date);

-- Daily actuals against a goal. One row per (goal, day). Value is what
-- the patient's app/device reported for that day (BiWaze sessions are
-- derived from therapy_sessions; others come from mobile-app self-report).

CREATE TABLE IF NOT EXISTS patient_goal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES patient_goals(id) ON DELETE CASCADE,
    log_date DATE NOT NULL,
    actual_value REAL NOT NULL,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(goal_id, log_date)
);
CREATE INDEX IF NOT EXISTS idx_goal_log_goal_date ON patient_goal_log(goal_id, log_date);

-- ═══════════════════════════════════════════════════════════════════
-- Business Associate Agreements (super-admin BAA tracking)
-- ═══════════════════════════════════════════════════════════════════
-- One row per BAA execution. Multiple rows allowed when a BAA is renewed —
-- the most recent unexpired row gates super-admin access to the org.

CREATE TABLE IF NOT EXISTS organization_baas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,                  -- relative to /static
    file_name TEXT,                            -- original upload filename for display
    signed_date DATE NOT NULL,                 -- when the customer signed the BAA
    effective_from DATE NOT NULL,
    expires_on DATE,                           -- NULL = no expiry on file (rare; demand a date)
    signed_by_name TEXT,
    signed_by_title TEXT,
    uploaded_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMP,                      -- super admin can mark a BAA revoked
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_baa_org ON organization_baas(organization_id, expires_on);


-- ═══════════════════════════════════════════════════════════════════
-- Parent-admin invitations (org onboarding)
-- ═══════════════════════════════════════════════════════════════════
-- Created when a super admin sets up a new organization. Holds the pending
-- parent-admin until they accept the invite (and create their user record).

CREATE TABLE IF NOT EXISTS org_invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    token TEXT NOT NULL UNIQUE,                -- random URL-safe token
    invited_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    invited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    accepted_at TIMESTAMP,
    accepted_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_invite_org ON org_invitations(organization_id, accepted_at);


-- ═══════════════════════════════════════════════════════════════════
-- Access log (HIPAA-adjacent audit trail)
-- ═══════════════════════════════════════════════════════════════════
-- Records who accessed / modified what, when. Viewable by admins for
-- their location; parent admins see all child locations. Events:
--   'patient_view'   — opened a patient detail page
--   'session_view'   — opened a therapy session detail
--   'report_generate'— rendered the patient therapy report
--   'data_export'    — any export action (future)
--   'task_activity'  — task status/assignment changes tied to alerts/messages

CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event TEXT NOT NULL,
    patient_id INTEGER REFERENCES patients(id) ON DELETE SET NULL,
    ref_type TEXT,      -- 'task' / 'alert' / 'inbox' / 'session' / 'report' etc.
    ref_id INTEGER,     -- FK by context; no enforced constraint so events stay even if target is deleted
    detail TEXT,
    -- 0 = customer-org user accessing their own data (default).
    -- 1 = ABMRC super admin accessing the customer's data; surfaced to the
    -- customer parent admin on a dedicated reciprocal audit view.
    is_external_access INTEGER NOT NULL DEFAULT 0,
    justification TEXT,                       -- minimum-necessary reason for super-admin PHI views
    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_access_org_time ON access_log(organization_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_access_patient ON access_log(patient_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_access_user ON access_log(user_id, occurred_at DESC);
