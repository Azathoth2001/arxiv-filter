-- arxiv-filter 初始 schema（schema_version = 1）
--
-- 设计约束来自 docs/data-schema.md §3：
--   * 时间一律以 TEXT 存 RFC 3339 UTC（`Z`），**不用** sqlite3 的隐式 timestamp converter；
--   * read_state 不设外键：条目可在 papers 之外独立存在（用户可能导入很久以前的已读记录）；
--   * query_results 依赖 query_cache 与 papers，删除缓存行时级联清理。
--
-- 迁移器负责在事务中执行本文件，成功后才会把 schema_meta.version 更新为 1。

CREATE TABLE schema_meta (
  version INTEGER NOT NULL
);

CREATE TABLE papers (
  canonical_id  TEXT PRIMARY KEY,   -- 不含末尾 vN
  metadata_json TEXT NOT NULL,      -- Paper 的 JSON 序列化
  updated_at    TEXT NOT NULL       -- RFC3339 Z
);

CREATE TABLE query_cache (
  cache_key            TEXT PRIMARY KEY,
  canonical_query_json TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  expires_at           TEXT NOT NULL,
  complete             INTEGER NOT NULL CHECK (complete IN (0,1)),
  byte_size            INTEGER NOT NULL,
  last_accessed_at     TEXT NOT NULL
);

CREATE TABLE query_results (
  cache_key    TEXT NOT NULL REFERENCES query_cache(cache_key) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  canonical_id TEXT NOT NULL REFERENCES papers(canonical_id),
  source_rank  INTEGER,
  score        REAL,
  PRIMARY KEY (cache_key, canonical_id)
);

CREATE TABLE query_history (
  id               TEXT PRIMARY KEY,
  raw_request_json TEXT NOT NULL,
  cache_key        TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  status           TEXT NOT NULL,
  result_count     INTEGER,
  cache_hit        INTEGER NOT NULL CHECK (cache_hit IN (0,1))
);

CREATE TABLE read_state (
  canonical_id  TEXT PRIMARY KEY,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  read_at       TEXT
);

CREATE INDEX idx_results_ordinal ON query_results(cache_key, ordinal);
CREATE INDEX idx_cache_expires   ON query_cache(expires_at);
CREATE INDEX idx_cache_accessed  ON query_cache(last_accessed_at);
CREATE INDEX idx_history_started ON query_history(started_at DESC);
