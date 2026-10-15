-- M7 Leaf Radar: regional pest and disease reports collected from extension services.

-- The US state a cell is in (from the National Weather Service's point lookup, kept for good),
-- so the weekly collector knows which regions have gardens.
ALTER TABLE cells ADD COLUMN region TEXT;

-- One row per report Leaf extracted. Every row has the page it came from and an exact quote
-- from that page, checked in code. Nothing is seeded or written by a model without a quote.
CREATE TABLE radar_reports (
  id TEXT PRIMARY KEY,
  region TEXT NOT NULL,               -- US state code, e.g. MN
  pest TEXT NOT NULL,                 -- the pest or disease as the page names it
  pest_key TEXT NOT NULL,             -- normalised words, for matching a diagnosis
  hosts TEXT NOT NULL DEFAULT '[]',   -- plants the page says are affected
  place TEXT NOT NULL DEFAULT '',     -- where, as the page states it ("Dakota County")
  status TEXT NOT NULL CHECK (status IN ('confirmed', 'reported')),
  reported_on TEXT NOT NULL,          -- the page's own date (YYYY-MM-DD), found in code
  quote TEXT NOT NULL,                -- verbatim from the page
  source_title TEXT NOT NULL,
  source_url TEXT NOT NULL,
  week TEXT NOT NULL,                 -- ISO week of the collection run
  collected_at INTEGER NOT NULL,
  trace_id TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX radar_once ON radar_reports (region, source_url, pest_key);
CREATE INDEX radar_region ON radar_reports (region, reported_on);

-- What each weekly run did, so an empty page says "checked, found nothing" rather than nothing.
CREATE TABLE radar_runs (
  region TEXT NOT NULL,
  week TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('done', 'skipped', 'failed')),
  searched INTEGER NOT NULL DEFAULT 0,   -- pages the searches returned
  read INTEGER NOT NULL DEFAULT 0,       -- pages fetched and read
  relevant INTEGER NOT NULL DEFAULT 0,   -- pages that reported a pest or disease occurring
  undated INTEGER NOT NULL DEFAULT 0,    -- relevant pages with no date Leaf could find
  rejected INTEGER NOT NULL DEFAULT 0,   -- extracted reports that failed the code checks
  reports INTEGER NOT NULL DEFAULT 0,    -- reports stored
  credits INTEGER NOT NULL DEFAULT 0,    -- Tavily credits spent
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  PRIMARY KEY (region, week)
);

-- Pages already read, so a weekly run does not pay to read the same page again.
CREATE TABLE radar_pages (
  url TEXT PRIMARY KEY,
  region TEXT NOT NULL,
  outcome TEXT NOT NULL,                 -- reports, none, undated, unsafe
  checked_at INTEGER NOT NULL
);
