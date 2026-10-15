-- M5 Season Plan. Each write of a garden's plan is its own row, so the verified plan stays
-- visible while a rewrite is in progress; when the new one is ready the older rows go.
-- Weeks are stored one row each, so a replan rewrites only the weeks it touches. What
-- changed and why is logged in `events` (kind 'replan').

CREATE TABLE season_plans (
  id TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  revision INTEGER NOT NULL DEFAULT 0,  -- +1 on every replan of this plan
  status TEXT NOT NULL DEFAULT 'writing'
    CHECK (status IN ('writing', 'unverified', 'ready', 'failed')),
  first_week TEXT NOT NULL,            -- Monday of the first planned week (YYYY-MM-DD)
  last_week TEXT NOT NULL,
  basis TEXT NOT NULL DEFAULT '{}',    -- frost dates, zone, plants and the outline it was built on
  checks TEXT NOT NULL DEFAULT '{}',   -- who wrote and verified it, what the checks removed
  note TEXT NOT NULL DEFAULT '',       -- why a write failed, in plain words
  trace_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX season_plans_garden ON season_plans (garden_id, created_at);

CREATE TABLE plan_weeks (
  plan_id TEXT NOT NULL REFERENCES season_plans (id) ON DELETE CASCADE,
  week TEXT NOT NULL,                  -- Monday of the week (YYYY-MM-DD)
  items TEXT NOT NULL DEFAULT '[]',    -- checked items, each with its reason and sources
  removed TEXT NOT NULL DEFAULT '[]',  -- items the code rules removed, with the reason
  revision INTEGER NOT NULL DEFAULT 0, -- the plan revision that last wrote this week
  changed TEXT NOT NULL DEFAULT '',    -- why this week last changed (empty if never replanned)
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (plan_id, week)
);

-- A diagnosis linked to a garden is an event (kind 'diagnosis', ref = case id); check-ins
-- find their garden through it.
CREATE INDEX events_ref ON events (kind, ref);
