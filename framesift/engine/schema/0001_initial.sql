-- Framesift catalog schema, version 1. See ARCHITECTURE.md §6.

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE hosts (
  hostname    TEXT PRIMARY KEY,
  platform    TEXT NOT NULL,
  source_path TEXT NOT NULL,
  review_path TEXT NOT NULL,
  delete_path TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE files (
  id              INTEGER PRIMARY KEY,
  root            TEXT NOT NULL CHECK (root IN ('source','review','delete')),
  rel_path        TEXT NOT NULL,
  rel_path_os     TEXT,
  origin_rel_path TEXT,
  dir_path        TEXT NOT NULL,
  name            TEXT NOT NULL,
  stem            TEXT NOT NULL,
  ext             TEXT NOT NULL,
  kind            TEXT NOT NULL,
  size            INTEGER NOT NULL,
  mtime_ns        INTEGER NOT NULL,
  item_id         INTEGER,
  companion_role  TEXT,
  edited_of       INTEGER,
  detached_at     TEXT,
  status          TEXT NOT NULL DEFAULT 'present',
  error           TEXT,
  reviewed_at     TEXT,
  first_seen      TEXT NOT NULL,
  last_seen       TEXT NOT NULL,
  seen_gen        INTEGER NOT NULL DEFAULT 0,
  UNIQUE (root, rel_path)
);
CREATE INDEX files_item      ON files(item_id);
CREATE INDEX files_root_kind ON files(root, kind, status);
CREATE INDEX files_size      ON files(size);
CREATE INDEX files_dir       ON files(root, dir_path);

CREATE TABLE file_meta (
  file_id        INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  size           INTEGER NOT NULL,
  mtime_ns       INTEGER NOT NULL,
  format         TEXT,
  width          INTEGER,
  height         INTEGER,
  orientation    INTEGER,
  make           TEXT,
  model          TEXT,
  software       TEXT,
  date_taken     TEXT,
  date_taken_ts  INTEGER,
  date_source    TEXT,
  user_comment   TEXT,
  content_id     TEXT,
  duration_ms    INTEGER,
  codec          TEXT,
  bitrate        INTEGER,
  fps            REAL,
  has_preview    INTEGER,
  preview_kind   TEXT,
  preview_offset INTEGER,
  preview_length INTEGER,
  camera_known   INTEGER NOT NULL DEFAULT 0,
  structure_ok   INTEGER,
  extractor      TEXT NOT NULL,
  extracted_at   TEXT NOT NULL
);
CREATE INDEX file_meta_content ON file_meta(content_id);
CREATE INDEX file_meta_date    ON file_meta(date_taken_ts);

CREATE TABLE file_analysis (
  file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  size         INTEGER NOT NULL,
  mtime_ns     INTEGER NOT NULL,
  partial_hash TEXT,
  full_hash    TEXT,
  phash        INTEGER,
  phash_source TEXT,
  sharpness    REAL,
  brightness   REAL,
  dark_p99     REAL,
  decode_error TEXT,
  analyzed_at  TEXT
);
CREATE INDEX file_analysis_partial ON file_analysis(partial_hash);
CREATE INDEX file_analysis_full    ON file_analysis(full_hash);

CREATE TABLE sessions (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  host       TEXT NOT NULL,
  app        TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at   TEXT,
  params     TEXT
);

CREATE TABLE jobs (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER REFERENCES sessions(id),
  kind        TEXT NOT NULL,
  state       TEXT NOT NULL,
  params      TEXT,
  progress    TEXT,
  checkpoint  TEXT,
  started_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  finished_at TEXT,
  error       TEXT
);

CREATE TABLE candidates (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL,
  file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  category      TEXT NOT NULL,
  confidence    TEXT NOT NULL,
  reason_key    TEXT NOT NULL,
  reason_params TEXT,
  group_id      TEXT,
  is_keeper     INTEGER NOT NULL DEFAULT 0,
  also          TEXT,
  applied_op    TEXT,
  UNIQUE (run_id, file_id)
);
CREATE INDEX candidates_cat ON candidates(run_id, category);

CREATE TABLE journal (
  id            INTEGER PRIMARY KEY,
  op_id         TEXT NOT NULL,
  session_id    INTEGER NOT NULL REFERENCES sessions(id),
  ts            TEXT NOT NULL,
  action        TEXT NOT NULL,
  file_id       INTEGER NOT NULL,
  item_id       INTEGER NOT NULL,
  from_root     TEXT,
  from_rel      TEXT,
  to_root       TEXT,
  to_rel        TEXT,
  method        TEXT,
  conflict      INTEGER NOT NULL DEFAULT 0,
  category      TEXT,
  reason_key    TEXT,
  reason_params TEXT,
  size          INTEGER NOT NULL DEFAULT 0,
  undoes        INTEGER REFERENCES journal(id),
  undone_by     INTEGER REFERENCES journal(id)
);
CREATE INDEX journal_session  ON journal(session_id);
CREATE INDEX journal_file     ON journal(file_id);
CREATE INDEX journal_category ON journal(category);
CREATE INDEX journal_op       ON journal(op_id);

CREATE TABLE issues (
  id      INTEGER PRIMARY KEY,
  job_id  INTEGER,
  ts      TEXT NOT NULL,
  path    TEXT,
  kind    TEXT NOT NULL,
  message TEXT
);
