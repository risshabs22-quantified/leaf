-- M3 plant knowledge graph (docs/LEAF_PLATFORM.md). Species profiles are built once and
-- shared by every user; each fact keeps the passage and page it came from.

CREATE TABLE species (
  key TEXT PRIMARY KEY,           -- "solanum lycopersicum", or a common name when that is all we have
  scientific_name TEXT NOT NULL DEFAULT '',
  common_names TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'ready', 'failed')),
  sources TEXT NOT NULL DEFAULT '[]',
  error TEXT NOT NULL DEFAULT '',
  trace_id TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  started_at INTEGER,             -- last build attempt: the daily cap counts attempts
  built_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX species_started ON species (started_at);

-- One row per extracted fact. verified: the quote is in the passage (checked in code) and
-- Super agreed it supports the value. rejected: one of those checks failed (kept for audit,
-- never shown as a fact). reference: from Leaf's cited tables (FAO-56 etc.), not extraction.
CREATE TABLE facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  species TEXT NOT NULL REFERENCES species (key) ON DELETE CASCADE,
  field TEXT NOT NULL,
  value TEXT NOT NULL,            -- JSON
  quote TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  source_title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('verified', 'rejected', 'reference')),
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX facts_species ON facts (species, field);

-- A photo identification awaiting the gardener's one-tap confirmation. The photo itself is
-- not kept; only what the models said about it.
CREATE TABLE identifications (
  id TEXT PRIMARY KEY,
  candidates TEXT NOT NULL DEFAULT '[]',
  features TEXT NOT NULL DEFAULT '[]',
  chosen TEXT,
  trace_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX identifications_created ON identifications (created_at);
