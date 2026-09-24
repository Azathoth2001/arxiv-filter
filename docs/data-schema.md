# arxiv-filter 数据契约（docs/data-schema.md）

> 状态：阶段 0 冻结（T0.1）；SQLite 实现在 T2.1 落地，HTTP DTO 在 T4.2 落地，导出 schema 在 T6.1 落地。
> 决策来源：`docs/decisions.md`。本文件描述**数据形状与语义**，不复述决策理由。

---

## 0. 时间与日期语义（冻结）

| 规则 | 值 |
| --- | --- |
| 内部时间类型 | 带时区的 UTC `datetime`（`datetime.now(timezone.utc)`） |
| 数据库 / JSON 写法 | RFC 3339，UTC 用 `Z` 后缀，例：`2025-01-02T03:04:05Z` |
| sqlite3 隐式转换 | **禁用**（不注册 `detect_types` adapter/converter），时间一律以 TEXT 存取 |
| 用户日期输入 | 按 **UTC 自然日**解释，闭区间 |
| 区间起点 | 开始日 `00:00`（含） |
| 区间终点 | 截止日 `23:59`（含，实现写 `2359`） |
| 上游表达式 | `submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]` |
| 展示口径 | `published_at` 显示为「首次提交日（UTC）」；`updated_at` 单独展示 |

依据 D1–D7 与方案 §4.1。`is_new` / 「最近 N 天」等一切「新」的判断都折算到 UTC 自然日。

## 1. seen / read 状态语义（冻结，对应 D2）

| 字段 | 含义 | 写入时机 |
| --- | --- | --- |
| `first_seen_at` | 该论文首次出现在任何结果列表中 | 卡片首次进入 viewport，批量 `mark_seen` |
| `last_seen_at` | 最近一次出现 | 同上，取 max |
| `read_at` | 用户**主动**标记已读，或（开关开启时）打开 abs/PDF | 仅用户动作 |

不变式：

1. `mark_seen` 绝不覆盖已存在的 `read_at`，只推进 `last_seen_at`。
2. 撤销已读只把 `read_at` 置 NULL，不改 `seen_at`。
3. 某个 job 的 `is_new` 按 **job 开始时** 的 `read_state` 快照计算；job 运行期间写入的 seen 不改变本次结果集的 `is_new`。
4. `read_filter` 语义：`all` = 不过滤；`new` = 按本次快照的 `is_new`；`unread` = 按当前 `read_at IS NULL`。
5. 导入合并规则：`first_seen=min`、`last_seen=max`、`read_at=max(non-null)`。

## 2. 结果上限与截断（冻结，对应 D3）

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `result_cap` | int，1..2000，默认 2000 | 本次 job 最多返回的去重后论文数 |
| `truncated` | bool | `totalResults > result_cap` |
| `partial` | bool | job 被取消或分片中途中止，当前结果不完整 |
| `target` | int \| null | `min(totalResults, result_cap)`；合并分片时为各分片之和的预计值 |

规则：`partial=true` 或 `cancelled` 的结果**只存在于当前 job 内存**，绝不写 `complete=1` 缓存。
进度文案在合并分片场景必须写「预计」。

## 3. SQLite schema（目标形状，T2.1 实现）

启动 PRAGMA：`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`。
所有写入经进程内 `asyncio.Lock`，在线程中执行 `BEGIN IMMEDIATE` 短事务；**不跨 await 持有事务**。

```sql
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
  cache_key   TEXT NOT NULL REFERENCES query_cache(cache_key) ON DELETE CASCADE,
  ordinal     INTEGER NOT NULL,
  canonical_id TEXT NOT NULL REFERENCES papers(canonical_id),
  source_rank INTEGER,
  score       REAL,
  PRIMARY KEY (cache_key, canonical_id)
);

CREATE TABLE query_history (
  id               TEXT PRIMARY KEY,   -- UUIDv7 文本
  raw_request_json TEXT NOT NULL,
  cache_key        TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  status           TEXT NOT NULL,      -- completed|cancelled|failed
  result_count     INTEGER,
  cache_hit        INTEGER NOT NULL CHECK (cache_hit IN (0,1))
);

CREATE TABLE read_state (
  canonical_id  TEXT PRIMARY KEY,      -- 不设外键：条目可在 papers 之外独立存在
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  read_at       TEXT
);

CREATE INDEX idx_results_ordinal  ON query_results(cache_key, ordinal);
CREATE INDEX idx_cache_expires    ON query_cache(expires_at);
CREATE INDEX idx_cache_accessed   ON query_cache(last_accessed_at);
CREATE INDEX idx_history_started  ON query_history(started_at DESC);
```

硬约束：

- `read_state` **不参与** LRU 清理，任何容量回收都不得删除它。
- `query_cache.complete=0` 的行可被安全丢弃重建；`read_state` 与 `query_history` 不得静默丢失。
- 缓存 schema 不兼容时可整体重建；`read_state` / history 只能通过 migration 或显式导入迁移。
- 删除 `query_cache` 行时经 `ON DELETE CASCADE` 清理 `query_results`。
- 迁移前用 `sqlite3.Connection.backup()` 写备份并验证可重新打开；保留最近 3 个已验证备份。
- 数据库 schema 版本比程序新时：**拒绝写入**，以只读模式启动并提示使用匹配版本或恢复备份。

## 4. 缓存键（冻结）

`cache_key = sha256(canonical_json)`，其中：

```python
payload = {
    "query_semantics_version": 1,   # 语义变更时递增，令旧缓存整体失效
    "provider": "arxiv_api",
    "category_ids": sorted(category_ids),   # 已 canonicalize（alias 已解析）
    "start_utc": "2025-01-01T00:00:00Z",
    "end_utc":   "2025-01-07T23:59:00Z",
    "keyword_ast": canonical_ast,           # 归一化后的 AST，不是用户原始字符串
    "scope": "all",
    "sort": "submitted_desc",
    "result_cap": 2000,
    "ranking_weights_version": 1,           # 本地相关度权重进 key
    "alias_canon_version": 1,               # taxonomy alias 映射版本进 key
}
canonical_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

不变式：JSON 字段书写顺序不影响 key；只有上述字段参与 key；`allow_stale_cache`、`read_filter`
等**展示层**参数**不**进 key（它们不改变上游请求）。

## 5. 领域模型（目标形状，T1.1 实现）

```python
class SearchScope(StrEnum):   ALL="all"; TITLE="title"; ABSTRACT="abstract"; AUTHOR="author"
class SortMode(StrEnum):      SUBMITTED_DESC="submitted_desc"; RELEVANCE="relevance"; UPDATED_DESC="updated_desc"

class Paper(BaseModel):
    schema_version: Literal[1] = 1
    canonical_id: str          # 不含末尾 vN；旧式 ID 保留斜杠
    versioned_id: str          # 返回值有 vN 时保留，否则等于 canonical_id
    version: int | None
    title: str
    authors: list[Author]      # Author(name, affiliation|None)，保持原顺序
    summary_raw: str           # 原始 LaTeX 文本；永不以 HTML 信任
    published_at: datetime     # Atom <published>，首次提交时间
    updated_at: datetime
    primary_category: str | None      # 缺失即 None + 解析警告，不猜第一个 category
    categories: list[str]             # 仅本地 taxonomy 中的 arXiv 分类 ID
    external_categories: list[str]    # ACM/MSC 等，单独保留
    comment / journal_ref / doi: str | None
    abs_url: str
    pdf_url: str | None
    retrieved_at: datetime
    source: Literal["arxiv_api","arxiv_oai","semantic_scholar","openalex","crossref"] = "arxiv_api"

class PaperView(Paper):
    first_seen_at: datetime | None
    read_at: datetime | None
    is_new: bool               # 按 job 开始快照
    is_read: bool
    relevance_score: float | None
```

`SearchRequest`（HTTP DTO 与内部共用）：

| 字段 | 类型 | 默认 | 校验 |
| --- | --- | --- | --- |
| `group_id` | str | — | 必须在 taxonomy 白名单中，否则中文 422 |
| `category_id` | str \| None | None | None = 该 group 全部叶子分类（D7） |
| `start_date` / `end_date` | date | — | `start_date <= end_date` |
| `keywords` | str | `""` | ≤ 500 字符 |
| `scope` | SearchScope | `all` | — |
| `sort` | SortMode | `submitted_desc` | — |
| `read_filter` | `all`\|`new`\|`unread` | `all` | 不进 cache key |
| `result_cap` | int | 2000 | 1..2000 |
| `allow_stale_cache` | bool | false | 不进 cache key |

时间字段一律用 `UtcDatetime`：**拒绝 naive datetime**（naive 值会让本机时区被静默
当成 UTC，在跨日筛选上产生错误结果）；JSON 序列化为 RFC 3339 `Z`。

`FeedPage`（T1.4 起）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `total_results` | int \| None | `opensearch:totalResults`；**缺失时为 `None`**，前端据此显示不定进度而不是编造 0 |
| `start_index` | int | `opensearch:startIndex`，缺失按 0 |
| `items_per_page` | int | `opensearch:itemsPerPage`，缺失按实际条数 |
| `papers` | list[Paper] | 解析成功的记录 |
| `feed_updated_at` | datetime \| None | `feed/updated` |
| `warnings` | list[str] | 解析期问题（缺 `primary_category`、跳过坏 ID 等），由 JobManager 提升到 `SearchJob.warnings` |

## 5.1 taxonomy 运行时契约（T1.x）

`src/arxiv_filter/taxonomy.py`：

- `load_taxonomy()` —— 带缓存；**加载即校验**（schema 版本、ID 唯一、父子引用、
  别名目标存在且不得多级链），失败抛 `TAXONOMY_CORRUPT`，**不联网临时抓页面**；
- `resolve_categories(group_id, category_id)` —— `category_id=None` 表示
  「全部子类」；返回**规范** ID 的排序去重元组；未知 ID 抛中文 422。

关键语义：`cs` 组的官方条目 `cs.SY` 是 `eess.SY` 的别名。因此
`resolve_categories("cs", None)` 的结果**包含 `eess.SY`**（它就是该组的
Systems and Control 分类，只是换了规范名），但**绝不包含 `cs.SY`**。
别名永远不进入 `search_query`。

## 6. HTTP 错误信封（冻结，T4.2 实现）

```json
{
  "error": {
    "code": "ARXIV_TIMEOUT",
    "message_zh": "连接 arXiv 超时，已完成重试。可再次尝试或使用上次缓存。",
    "message_en": "The arXiv request timed out after retries.",
    "retryable": true,
    "request_id": "01J...",
    "details": {}
  }
}
```

规则：每个 4xx/5xx 都必须带 `code`、`message_zh`、`request_id`；`code` 一经发布不得改义；
`details` 不得包含代理凭据或完整请求 URL 的用户名/密码。

## 7. read_state 导入 / 导出 JSON

```json
{
  "schema_version": 1,
  "exported_at": "2026-09-22T00:00:00Z",
  "app_version": "0.1.0",
  "entries": [
    {"canonical_id": "2501.01234", "first_seen_at": "...", "last_seen_at": "...", "read_at": null}
  ]
}
```

合并按 `canonical_id`：`first_seen=min`、`last_seen=max`、`read_at=max(non-null)`。

## 8. 导出统一字段顺序（冻结，T6.* 实现）

`canonical_id, versioned_id, title, authors, published_utc, updated_utc, primary_category,
categories, external_categories, summary, comment, journal_ref, doi, abs_url, pdf_url,
is_new, is_read, relevance_score`

导出 JSON 顶层：`{"schema_version":"1.0","query":{...},"exported_at":"...Z","papers":[...]}`，
UTF-8、`ensure_ascii=false`、RFC 3339 UTC。

文件名：`arxiv_<category>_<YYYYMMDD>-<YYYYMMDD>_<keyword-24chars>.<ext>`，
删除 `<>:"/\|?*` 与尾部点/空格与保留设备名，基础名 ≤120 字符，碰撞追加 `_HHMMSS`。

## 9. taxonomy 快照（T0.3 产出）

`src/arxiv_filter/data/categories.zh-CN.json` 顶层字段：

```json
{
  "schema_version": 1,
  "taxonomy_version": "YYYY-MM-DD",
  "source": {"html_url": "...", "html_sha256": "...", "html_checked_at": "...",
             "canonical_url": "...", "canonical_sha256": "...", "canonical_checked_at": "...",
             "canonical_commit": "..."},
  "aliases": {"cs.SY": "eess.SY", "...": "..."},
  "groups":     [{"id": "...", "name_en": "...", "name_zh": "..."}],
  "archives":   [{"id": "...", "group_id": "...", "name_en": "...", "name_zh": "..."}],
  "categories": [{"id": "...", "archive_id": "...", "group_id": "...",
                  "name_en": "...", "name_zh": "...", "active": true, "alias_of": null}]
}
```

规则：

- 运行时不抓网页；前端只读本地快照。
- 中文名**不参与** API query；query 只用 `id`。
- `alias_of` 非 null 表示该 ID 是非规范别名，构造 query 时先解析为规范 ID。
- 不假定 `cat:cs.*` 之类未确认的通配语法；「全部子类」展开为精确 category OR。
- 编码后 URL 超过 1800 字符时，MVP 安全路径是拆成多个顺序 GET（共享同一 limiter）后去重。
