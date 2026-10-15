-- Leaf platform foundation (docs/LEAF_PLATFORM.md, "Data model").
-- Applied by Wrangler to D1 (`migrations_dir` in cloudflare/wrangler.jsonc) and by
-- backend/data/migrate.py to SQLite locally; both record it in d1_migrations.
-- Times are Unix seconds; JSON columns are TEXT.

-- No accounts: a garden is opened by a private link whose key only exists as a SHA-256 here.
CREATE TABLE gardens (
  id TEXT PRIMARY KEY,
  key_hash TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  lat REAL,                       -- rounded to 0.01 degrees (about 1 km) before it is stored
  lon REAL,
  cell TEXT,                      -- 0.1-degree grid cell shared by forecasts and climatology
  timezone TEXT NOT NULL DEFAULT 'UTC',
  plan_hour_utc INTEGER,          -- UTC hour when it is 05:00 in the garden (morning planning)
  prefs TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);
CREATE INDEX gardens_cell ON gardens (cell);
CREATE INDEX gardens_plan_hour ON gardens (plan_hour_utc, cell);

CREATE TABLE plants (
  id TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  species TEXT NOT NULL DEFAULT '',
  common_name TEXT NOT NULL DEFAULT '',
  nickname TEXT NOT NULL DEFAULT '',
  -- Decides the water model: outdoor plants use the FAO-56 balance, indoor plants never do.
  setting TEXT NOT NULL DEFAULT 'outdoor' CHECK (setting IN ('outdoor', 'indoor')),
  container TEXT NOT NULL DEFAULT '{}',
  planted_on TEXT,
  stage TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX plants_garden ON plants (garden_id);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  plant_id TEXT REFERENCES plants (id) ON DELETE CASCADE,
  day TEXT NOT NULL,              -- the garden's local date (YYYY-MM-DD)
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '{}',
  due_at INTEGER,
  urgency TEXT NOT NULL DEFAULT 'normal' CHECK (urgency IN ('normal', 'soon', 'urgent')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'skipped', 'expired')),
  created_by TEXT NOT NULL DEFAULT 'rule',
  trace_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
-- One task of a kind per plant per day, so re-running the morning planner adds nothing.
CREATE UNIQUE INDEX tasks_once ON tasks (garden_id, IFNULL(plant_id, ''), kind, day);
CREATE INDEX tasks_due ON tasks (garden_id, status, day);

-- What Leaf recommended and what the gardener did, so plans can be checked and improved.
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  plant_id TEXT,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  trace_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX events_garden ON events (garden_id, created_at);

CREATE TABLE notification_settings (
  garden_id TEXT PRIMARY KEY REFERENCES gardens (id) ON DELETE CASCADE,
  channels TEXT NOT NULL DEFAULT '[]',
  quiet_start TEXT NOT NULL DEFAULT '21:00',
  quiet_end TEXT NOT NULL DEFAULT '07:00',
  max_per_day INTEGER NOT NULL DEFAULT 3,
  updated_at INTEGER NOT NULL
);

-- 30-day retention: housekeeping deletes the R2 object, then the row.
CREATE TABLE photos (
  id TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  plant_id TEXT,
  r2_key TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX photos_expiry ON photos (expires_at);

-- The queue delivers at least once; this table makes each job run effectively once.
CREATE TABLE jobs (
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  lease_until INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX jobs_status ON jobs (status, updated_at);

-- The meter: one row per model call (kept 30 days).
CREATE TABLE model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,
  trace_id TEXT NOT NULL DEFAULT '',
  task TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE INDEX model_calls_day ON model_calls (day, task);

CREATE TABLE cache_stats (
  day TEXT NOT NULL,
  namespace TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0,
  misses INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, namespace)
);

-- Active users for cost per user: a garden id, or a visitor hashed with a salt that exists
-- for one day and is deleted after two, so the hashes cannot be reversed later.
CREATE TABLE active_daily (
  day TEXT NOT NULL,
  actor TEXT NOT NULL,
  PRIMARY KEY (day, actor)
);

CREATE TABLE day_salts (
  day TEXT PRIMARY KEY,
  salt TEXT NOT NULL
);
