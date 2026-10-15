-- M4 Leaf Care: notifications (dedupe, quiet hours, rate limits) and Web Push subscriptions.

-- One row per notification Leaf decided to send. `dedupe_key` (unique) means the same
-- reminder or alert is never sent twice, however often a job runs.
CREATE TABLE notifications (
  id TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  dedupe_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,             -- morning, alert, test
  urgent INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '/today',
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'skipped', 'failed')),
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  sent_at INTEGER
);
CREATE INDEX notifications_garden ON notifications (garden_id, sent_at);

-- A browser's Web Push subscription for a garden (endpoint plus its keys). Endpoints the
-- push service reports gone (404/410) are deleted.
CREATE TABLE push_subscriptions (
  id TEXT PRIMARY KEY,
  garden_id TEXT NOT NULL REFERENCES gardens (id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_ok INTEGER,
  failures INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX push_garden ON push_subscriptions (garden_id);
