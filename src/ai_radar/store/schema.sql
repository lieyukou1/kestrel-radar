PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  name          TEXT NOT NULL,
  authority     TEXT NOT NULL,
  region        TEXT NOT NULL DEFAULT 'domestic',
  config_json   TEXT NOT NULL,
  cursor_json   TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1,
  last_run_at   TEXT,
  last_error    TEXT
);

CREATE TABLE IF NOT EXISTS raw_items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id     TEXT NOT NULL REFERENCES sources(id),
  external_id   TEXT NOT NULL,
  url           TEXT NOT NULL,
  title         TEXT,
  author        TEXT,
  published_at  TEXT,
  fetched_at    TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  UNIQUE(source_id, external_id)
);

CREATE TABLE IF NOT EXISTS documents (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_id  TEXT NOT NULL UNIQUE,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL,
  abstract      TEXT,
  url           TEXT NOT NULL,
  published_at  TEXT,
  first_seen_at TEXT NOT NULL,
  authority     TEXT NOT NULL,
  status        TEXT NOT NULL,
  primary_tag   TEXT,
  tags_json     TEXT,
  read_at       TEXT,
  read_count    INTEGER NOT NULL DEFAULT 0,
  local_path    TEXT,
  merged_into   INTEGER REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS document_sources (
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  raw_item_id   INTEGER NOT NULL REFERENCES raw_items(id),
  PRIMARY KEY (document_id, raw_item_id)
);

CREATE TABLE IF NOT EXISTS signals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  source_id     TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  points        INTEGER,
  comments      INTEGER,
  stars         INTEGER,
  citations     INTEGER,
  extra_json    TEXT
);

CREATE TABLE IF NOT EXISTS digests (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  digest_date   TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  sent_at       TEXT,
  status        TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'daily',
  stats_json    TEXT,
  -- 同一天可以既有周报又有日报（周一），按 kind 区分
  UNIQUE(digest_date, kind)
);

CREATE TABLE IF NOT EXISTS digest_items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  digest_id     INTEGER NOT NULL REFERENCES digests(id),
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  rank          INTEGER NOT NULL,
  score         REAL NOT NULL,
  reason        TEXT,
  section       TEXT NOT NULL DEFAULT 'brief',
  summary_json  TEXT,
  UNIQUE(digest_id, document_id)
);

CREATE TABLE IF NOT EXISTS topics (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  digest_id     INTEGER REFERENCES digests(id),
  topic_date    TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'daily',
  label         TEXT,
  summary       TEXT,
  entity        TEXT,
  dimension     TEXT,
  score         REAL NOT NULL DEFAULT 0,
  item_count    INTEGER NOT NULL DEFAULT 0,
  -- 事件本身发生/发布的时间，用来判断下次是「新进展」还是「同一件事的回声」
  event_date    TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_items (
  topic_id      INTEGER NOT NULL REFERENCES topics(id),
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  angle         TEXT,
  rank          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (topic_id, document_id)
);

CREATE TABLE IF NOT EXISTS interactions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  digest_id     INTEGER REFERENCES digests(id),
  action        TEXT NOT NULL,
  reason        TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(document_id, digest_id, action)
);

CREATE TABLE IF NOT EXISTS preferences (
  key           TEXT PRIMARY KEY,
  value_json    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  job           TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,
  stats_json    TEXT,
  error         TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  day           TEXT NOT NULL,
  stage         TEXT NOT NULL,
  model         TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_cny      REAL NOT NULL,
  document_id   INTEGER REFERENCES documents(id),
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
  event_id      TEXT PRIMARY KEY,
  received_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_articles (
  document_id   INTEGER PRIMARY KEY REFERENCES documents(id),
  problem       TEXT,
  method        TEXT,
  evidence      TEXT,
  conclusions   TEXT,
  limitations   TEXT,
  card_json     TEXT NOT NULL,
  deep_read_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_vectors (
  document_id   INTEGER PRIMARY KEY REFERENCES documents(id),
  model         TEXT NOT NULL,
  dim           INTEGER NOT NULL,
  vector_json   TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  section       TEXT,
  ord           INTEGER NOT NULL,
  text          TEXT NOT NULL,
  anchor        TEXT
);

CREATE TABLE IF NOT EXISTS claims (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  chunk_id      INTEGER REFERENCES chunks(id),
  kind          TEXT NOT NULL,
  text          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  from_doc      INTEGER NOT NULL REFERENCES documents(id),
  to_doc        INTEGER NOT NULL REFERENCES documents(id),
  kind          TEXT NOT NULL,
  evidence      TEXT
);

CREATE TABLE IF NOT EXISTS notes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  content       TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status, published_at);
CREATE INDEX IF NOT EXISTS idx_documents_read ON documents(read_at);
CREATE INDEX IF NOT EXISTS idx_signals_doc_time ON signals(document_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_raw_items_source ON raw_items(source_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_interactions_doc ON interactions(document_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job, started_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_day ON llm_usage(day, stage);
CREATE INDEX IF NOT EXISTS idx_topics_date ON topics(topic_date, kind);
CREATE INDEX IF NOT EXISTS idx_topic_items_doc ON topic_items(document_id);
CREATE INDEX IF NOT EXISTS idx_notes_doc ON notes(document_id, created_at);
