-- M6 Connect: API keys with per-key limits, Home Assistant sensor readings, and Telegram chats.

-- A key belongs to a garden and acts for it. Only its SHA-256 is stored; the secret is shown
-- once when it is made. Scopes say what it may do: read, readings, tools.
CREATE TABLE api_keys (
  id TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  key_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,                 -- the first characters, so a person can tell keys apart
  label TEXT NOT NULL DEFAULT '',
  scopes TEXT NOT NULL DEFAULT '["read"]',
  per_hour INTEGER NOT NULL DEFAULT 120,
  per_day INTEGER NOT NULL DEFAULT 1000,
  model_per_day INTEGER NOT NULL DEFAULT 20,  -- calls that may spend model credits
  created_at INTEGER NOT NULL,
  last_used INTEGER,
  revoked_at INTEGER
);
CREATE INDEX api_keys_garden ON api_keys (garden_id);

-- Counters per key and UTC bucket (h<yyyymmddhh>, d<yyyymmdd>, m<yyyymmdd>). In D1, never KV:
-- KV allows one write a second to a key and takes up to a minute to propagate.
CREATE TABLE api_key_hits (
  key_id TEXT NOT NULL REFERENCES api_keys (id) ON DELETE CASCADE,
  bucket TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key_id, bucket)
);

-- Soil moisture from a sensor (Home Assistant). A fresh reading replaces the water balance for
-- its plant. `label` is 'test' for a reading sent as a test, which Leaf says so wherever it
-- uses it; nothing a person types is ever shown as a real sensor reading.
CREATE TABLE sensor_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  plant_id TEXT NOT NULL REFERENCES plants (id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'soil_moisture',
  value REAL NOT NULL,                  -- percent
  dry_below REAL,                       -- the sender's own dry threshold, if it set one
  label TEXT NOT NULL DEFAULT 'sensor' CHECK (label IN ('sensor', 'test')),
  source TEXT NOT NULL DEFAULT '',
  key_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX readings_plant ON sensor_readings (plant_id, created_at);
CREATE INDEX readings_garden ON sensor_readings (garden_id, created_at);

-- A Telegram chat that receives a garden's notifications and can talk to Leaf.
CREATE TABLE telegram_chats (
  chat_id TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  last_ok INTEGER,
  failures INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX telegram_garden ON telegram_chats (garden_id);

-- One-time codes that link a chat to a garden (`/start <code>`); only their hash is stored.
CREATE TABLE telegram_links (
  code_hash TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL
);
