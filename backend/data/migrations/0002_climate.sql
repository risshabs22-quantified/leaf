-- M2 climate engine (docs/LEAF_PLATFORM.md). Shared per 0.1-degree cell, never per user.

-- Every cell that holds at least one garden. `us` is null until NWS has been asked once
-- (NWS answers only for US points).
CREATE TABLE cells (
  cell TEXT PRIMARY KEY,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  us INTEGER,
  created_at INTEGER NOT NULL
);

-- 1991-2020 normals for a cell, built once (about 780 weighted Open-Meteo calls) and kept.
CREATE TABLE climatology (
  cell TEXT PRIMARY KEY REFERENCES cells (cell) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'ready', 'failed')),
  data TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  built_at INTEGER,
  updated_at INTEGER NOT NULL
);
CREATE INDEX climatology_built ON climatology (status, built_at);

-- Weighted calls to free services per UTC day, so Leaf stays inside their limits.
CREATE TABLE api_usage (
  day TEXT NOT NULL,
  service TEXT NOT NULL,
  units REAL NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, service)
);
