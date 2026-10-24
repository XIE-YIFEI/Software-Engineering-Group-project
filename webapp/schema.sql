-- SECS Vision web system: SQLite schema (docs/system/DATABASE_EN.md explains every table).
-- Times are Malaysia time (MYT) as ISO 8601 text, see webapp/clock.py. `compact` = canonical plate (VJF1234),
-- `plate_text` = display form (VJF 1234). `provenance` keeps real and simulated data apart:
--   camera          a real vehicle with its real plate, read from the footage
--   inserted_plate  a real vehicle whose plate was digitally replaced by a fictitious one (violation demo)
--   synthetic       no vehicle in the footage at all (simulated events, simulated lane vehicles, gate arrivals)

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('road', 'condo', 'checkpoint', 'highway'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('police', 'guard', 'traffic', 'admin')),
    site_id INTEGER REFERENCES sites(id),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    demo INTEGER NOT NULL DEFAULT 0,          -- created from webapp/seed/demo_users.yaml
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,          -- SHA-256 of the cookie value; the token itself is never stored
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('anpr', 'gate', 'lane', 'congestion')),
    site_id INTEGER REFERENCES sites(id),
    lat REAL,
    lon REAL,
    location_note TEXT NOT NULL DEFAULT '',
    source_credit TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'replay' CHECK (status IN ('replay', 'live', 'offline')),
    stream_url TEXT,                          -- future: live RTSP source
    notional_clock INTEGER NOT NULL DEFAULT 0 -- times of its demo runs are notional (not the recording's)
);

-- One analysis of one video or image (a committed demo result or an upload).
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    camera_id INTEGER NOT NULL REFERENCES cameras(id),
    origin TEXT NOT NULL CHECK (origin IN ('demo', 'upload')),
    media TEXT NOT NULL DEFAULT 'video' CHECK (media IN ('video', 'image')),
    results_path TEXT NOT NULL,
    results_sha256 TEXT NOT NULL,
    video_path TEXT,                          -- what the browser plays (repo-relative or absolute)
    video_kind TEXT,                          -- annotated | source
    video_offset_sec REAL NOT NULL DEFAULT 0, -- source second at which the played video starts
    image_path TEXT,                          -- image runs: the annotated image
    profile TEXT,
    fps REAL,
    frames INTEGER,
    window_start_sec REAL,
    window_end_sec REAL,
    clock_origin TEXT NOT NULL,               -- local time of source second 0
    provenance_note TEXT NOT NULL DEFAULT '',
    flow_primary INTEGER NOT NULL DEFAULT 1,  -- counted in the camera's flow (two runs of one footage: one is)
    count_line_y REAL,
    flow_total INTEGER,
    flow_by_type TEXT,                        -- JSON
    registry_rules TEXT,                      -- JSON list, NULL = registry checks were off
    registry_disclaimer TEXT,
    processing TEXT,                          -- JSON (model backend, headless, FPS) when the results have it
    job_id INTEGER,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plates (           -- distinct plates, for the fuzzy search
    compact TEXT PRIMARY KEY,
    plate_text TEXT NOT NULL,
    length INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS plates_length ON plates (length);

CREATE TABLE IF NOT EXISTS sightings (        -- one vehicle (one plate-log row) seen by one run
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    camera_id INTEGER NOT NULL REFERENCES cameras(id),
    natural_key TEXT NOT NULL,                -- stable across re-imports, e.g. p:<first_frame>:<compact>
    compact TEXT NOT NULL REFERENCES plates(compact),
    plate_text TEXT NOT NULL,
    confidence REAL,
    votes INTEGER,
    status TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'TENTATIVE', 'ACQUIRING')),
    valid_format INTEGER NOT NULL DEFAULT 1,
    vtype TEXT,
    region TEXT,
    track_ids TEXT,                           -- JSON list
    first_frame INTEGER,
    last_frame INTEGER,
    first_sec REAL NOT NULL,
    last_sec REAL NOT NULL,
    seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (provenance IN ('camera', 'inserted_plate', 'synthetic')),
    crop_path TEXT,                           -- media-relative
    scene_path TEXT,                          -- media-relative
    scene_info TEXT,                          -- JSON: frame, time_sec, vehicle_box, plate_box
    recorded_at TEXT NOT NULL,                -- when the system stored it (retention counts from here)
    UNIQUE (run_id, natural_key)
);
CREATE INDEX IF NOT EXISTS sightings_compact ON sightings (compact);
CREATE INDEX IF NOT EXISTS sightings_seen ON sightings (seen_at);
CREATE INDEX IF NOT EXISTS sightings_camera ON sightings (camera_id);

CREATE TABLE IF NOT EXISTS lane_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    natural_key TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,                   -- info | hazard | violation
    time_sec REAL NOT NULL,
    seen_at TEXT NOT NULL,
    lane TEXT,
    from_lane TEXT,
    message TEXT NOT NULL,
    vtype TEXT,
    track_id INTEGER,
    compact TEXT,
    plate_text TEXT,
    provenance TEXT NOT NULL CHECK (provenance IN ('camera', 'inserted_plate', 'synthetic')),
    value REAL,
    error_1sigma REAL,
    UNIQUE (run_id, natural_key)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    natural_key TEXT NOT NULL UNIQUE,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    camera_id INTEGER REFERENCES cameras(id),
    sighting_id INTEGER REFERENCES sightings(id) ON DELETE CASCADE,
    lane_event_id INTEGER REFERENCES lane_events(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('registry', 'lane', 'list', 'gate')),
    code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('high', 'violation', 'hazard', 'medium')),
    rank INTEGER NOT NULL,                    -- 1 = high / violation, 2 = hazard, 3 = medium (sort order)
    message TEXT NOT NULL,
    compact TEXT,
    plate_text TEXT,
    seen_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (provenance IN ('camera', 'inserted_plate', 'synthetic')),
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'acknowledged', 'resolved', 'false_alarm')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts (status, rank, seen_at);
CREATE INDEX IF NOT EXISTS alerts_sighting ON alerts (sighting_id);
CREATE INDEX IF NOT EXISTS alerts_compact ON alerts (compact);

CREATE TABLE IF NOT EXISTS alert_actions (
    id INTEGER PRIMARY KEY,
    alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,                     -- created | acknowledge | resolve | false_alarm | reopen | note
    from_status TEXT,
    to_status TEXT,
    note TEXT NOT NULL DEFAULT '',
    corrected_plate TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lanes (            -- per-lane totals of a lane-camera run
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    grp TEXT NOT NULL,
    direction TEXT NOT NULL,
    position INTEGER NOT NULL,
    count INTEGER NOT NULL,
    by_type TEXT NOT NULL,                    -- JSON
    veh_per_min REAL,
    occupancy REAL,
    density REAL,
    density_unit TEXT,
    zone_length_m REAL,
    mean_speed_kmh REAL,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS lane_bins (        -- lanes.per_10s
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    lane TEXT NOT NULL,
    t_start REAL NOT NULL,
    t_end REAL NOT NULL,
    count INTEGER,
    veh_per_min REAL,
    occupancy REAL,
    mean_vehicles REAL,
    density_veh_km REAL,
    mean_speed_kmh REAL,
    observed_sec REAL,
    PRIMARY KEY (run_id, lane, t_start)
);

CREATE TABLE IF NOT EXISTS crossings (        -- lanes.crossings
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    time_sec REAL NOT NULL,
    seen_at TEXT NOT NULL,
    lane TEXT,
    vtype TEXT,
    track_id INTEGER,
    direction TEXT
);
CREATE INDEX IF NOT EXISTS crossings_run ON crossings (run_id, time_sec);

CREATE TABLE IF NOT EXISTS flow_bins (        -- flow.per_10s (counting line)
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    t_start REAL NOT NULL,
    t_end REAL NOT NULL,
    total INTEGER NOT NULL,
    by_type TEXT NOT NULL,                    -- JSON
    PRIMARY KEY (run_id, t_start)
);

CREATE TABLE IF NOT EXISTS withheld (         -- lanes.withheld_violations: counted, never attributed
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    count INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, code)
);

-- Congestion cameras (alpr/congestion.py): one row per camera-day series, per-minute values per zone / direction.
CREATE TABLE IF NOT EXISTS series_days (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    camera_id INTEGER NOT NULL REFERENCES cameras(id),
    label TEXT NOT NULL,
    date TEXT NOT NULL,
    csv_path TEXT NOT NULL,
    csv_sha256 TEXT NOT NULL,
    calibrated INTEGER NOT NULL DEFAULT 0,
    video_path TEXT,                          -- time-lapse of this day, if any
    video_from_minute INTEGER,                -- minute of day at video second 0
    video_to_minute INTEGER,
    video_duration_sec REAL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zone_minutes (
    day_id INTEGER NOT NULL REFERENCES series_days(id) ON DELETE CASCADE,
    entity TEXT NOT NULL,                     -- a zone (TC1 ...) or a direction (To CIQ)
    minute TEXT NOT NULL,
    minute_of_day INTEGER NOT NULL,
    occupancy REAL,
    excess REAL,
    density REAL,                             -- calibrated vehicles in the zone
    fill REAL,                                -- vehicles / jam capacity
    queue REAL,                               -- directions: zones with fill >= 0.5
    observed_sec REAL,
    PRIMARY KEY (day_id, entity, minute)
);

CREATE TABLE IF NOT EXISTS calibrations (
    day_id INTEGER NOT NULL REFERENCES series_days(id) ON DELETE CASCADE,
    entity TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('zone', 'group')),
    slope REAL,
    intercept REAL,
    night_offset REAL,
    n INTEGER,
    loo_mae REAL,
    loo_bias REAL,
    mean_count REAL,
    corr REAL,
    relative_mae REAL,
    capacity REAL,                            -- jam capacity (vehicles); a group: sum of its zones
    PRIMARY KEY (day_id, entity)
);

CREATE TABLE IF NOT EXISTS forecast_runs (
    id INTEGER PRIMARY KEY,
    day_id INTEGER NOT NULL REFERENCES series_days(id) ON DELETE CASCADE,
    entity TEXT NOT NULL,
    model TEXT NOT NULL,
    train_day TEXT NOT NULL,
    horizons TEXT NOT NULL,                   -- JSON [5, 15, 30]
    congestion_fill REAL NOT NULL,
    error_band TEXT NOT NULL,                 -- JSON {h: MAE on the held-out part of the training day}
    origin TEXT NOT NULL CHECK (origin IN ('committed', 'computed')),
    source_path TEXT,
    source_sha256 TEXT,
    UNIQUE (day_id, entity)
);

CREATE TABLE IF NOT EXISTS forecast_points (  -- f<h>: forecast made at this minute for minute + h
    run_id INTEGER NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
    minute TEXT NOT NULL,
    measured REAL,
    f5 REAL,
    f15 REAL,
    f30 REAL,
    PRIMARY KEY (run_id, minute)
);

-- Lists on top of the SIMULATED registry: guard lists (resident, banned) are per site, police lists are global.
CREATE TABLE IF NOT EXISTS list_entries (
    id INTEGER PRIMARY KEY,
    list TEXT NOT NULL CHECK (list IN ('resident', 'banned', 'watch', 'blacklist')),
    site_id INTEGER REFERENCES sites(id),
    compact TEXT NOT NULL,
    plate_text TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    unit_no TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    valid_to TEXT,
    simulated INTEGER NOT NULL DEFAULT 0,     -- demo entry from webapp/seed/
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS list_entries_compact ON list_entries (compact, list, active);

CREATE TABLE IF NOT EXISTS visitor_passes (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    compact TEXT NOT NULL,
    plate_text TEXT NOT NULL,
    unit_no TEXT NOT NULL,
    visitor_label TEXT NOT NULL DEFAULT '',   -- e.g. "Delivery"; no names or IC numbers
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    simulated INTEGER NOT NULL DEFAULT 0,
    seed_key TEXT UNIQUE,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passes_compact ON visitor_passes (compact, site_id);

CREATE TABLE IF NOT EXISTS access_events (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    camera_id INTEGER REFERENCES cameras(id),
    sighting_id INTEGER REFERENCES sightings(id) ON DELETE SET NULL,
    compact TEXT NOT NULL,
    plate_text TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('entry', 'exit')),
    decision TEXT NOT NULL CHECK (decision IN ('allowed', 'stopped', 'exited')),
    identity TEXT NOT NULL CHECK (identity IN ('resident', 'visitor_pre', 'visitor_unregistered', 'banned')),
    pass_id INTEGER REFERENCES visitor_passes(id),
    unit_no TEXT NOT NULL DEFAULT '',
    guard_id INTEGER REFERENCES users(id),
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0,
    seed_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS access_compact ON access_events (site_id, compact, at);

CREATE TABLE IF NOT EXISTS reveals (          -- "Show full plate" by a masked role
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    sighting_id INTEGER REFERENCES sightings(id) ON DELETE CASCADE,
    compact TEXT,
    reason_code TEXT NOT NULL,
    reason_note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Append-only: the triggers below reject UPDATE, and DELETE except during a retention purge; every row carries the
-- hash of the previous row (prev_hash -> row_hash), so a changed or removed row breaks the chain (GET /audit/verify).
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details TEXT NOT NULL DEFAULT '{}',       -- JSON: filters, result counts, via=assistant ...
    reason TEXT NOT NULL DEFAULT '',          -- purpose of a search / reason of a reveal
    ip TEXT,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_at ON audit_log (at);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
WHEN COALESCE((SELECT value FROM meta WHERE key = 'audit_purge'), '') != 'on'
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'video')),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    input_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    scene TEXT NOT NULL,
    render_video INTEGER NOT NULL DEFAULT 0,
    camera_id INTEGER NOT NULL REFERENCES cameras(id),
    recorded_at TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS corrections (      -- OCR correction pool (false alarms with the correct plate)
    id INTEGER PRIMARY KEY,
    alert_id INTEGER REFERENCES alerts(id) ON DELETE SET NULL,
    sighting_id INTEGER REFERENCES sightings(id) ON DELETE SET NULL,
    ocr_compact TEXT NOT NULL,
    corrected_compact TEXT NOT NULL,
    crop_path TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_exports (
    id INTEGER PRIMARY KEY,
    alert_id INTEGER REFERENCES alerts(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id),
    format TEXT NOT NULL,
    path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    image_hashes TEXT NOT NULL,               -- JSON {name: sha256}
    at TEXT NOT NULL
);
