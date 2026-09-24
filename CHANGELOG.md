# 变更记录

本文件同时记录**应用版本**（SemVer）与**数据迁移**（SQLite `schema_version` 单调递增）。
分类快照另有独立版本号，见 `src/arxiv_filter/data/categories.zh-CN.json` 的 `taxonomy_version`。

## [未发布]

### 阶段 0 · 决策与事实验证（T0.1–T0.4）

**T0.1 决策冻结**
- 新增 `docs/decisions.md`：冻结方案第 10 部分的 7 项决策（D1–D7），
  逐条给出唯一默认值、理由、被否方案、验收关联与影响面；
- 新增 `docs/data-schema.md`：冻结 UTC 日期闭区间语义、seen/read 分离语义、
  结果上限与截断语义、SQLite 目标 schema、缓存键构成、HTTP 错误信封、导出字段顺序。

**T0.2 API 探测**
- 新增 `scripts/probe_arxiv_api.py`，支持
  `smoke / redirect / fields / case-sensitivity / chinese / special / post / page-size /
  total-stability / window / all` 共 11 种模式；
- 所有请求（含探测重试）共用一个最小间隔限速器，并在 JSON 中输出
  实测最小起点间隔与是否符合限速要求；
- 探测成功判定不只看 200：检查 content-type、XML 可解析性、Atom `Error` entry、
  `opensearch:totalResults`、命名空间集合；
- 失败只保留 ≤ 1 KiB 的脱敏响应摘要（遮蔽 URL 凭据与 token/api_key/password 字段）；
- 记录 `via` / `x-cache` / `server` / `age` / `retry-after` 等 CDN 响应头，
  用于区分「上游限流」与「契约变化」。

**T0.2 实测发现（影响 T3.1/T3.2，见 `docs/api-verification.md`）**

- 🔴 **arXiv 用 HTTP 406 + 空响应体表达限流，而不是 429。** 方案 §4.8/§5.4 只覆盖了
  429 与 5xx，必须补 406 的长退避路径，且不得把 406 归因于用户的关键词语法。
- 🔴 **方案的 3.1 秒请求间隔不足。** 实测在 3.1 秒间隔下连续查询会触发 406；
  间隔放大到 8 秒、并在每次 406 后额外退避 30 秒，仍然复发。
  决策登记已把默认间隔改为 **4.0 秒**，并要求用 deadline 循环实现限速
  （Windows `time.sleep` 会提前返回，实测 3.1 秒目标只跑出 3.093 秒）。
- 🟡 已排除的原因：自定义 `Accept`/UA/`Accept-Encoding`（5 组对照全部 406）、
  `max_results` 取值、本机代理（环境变量/注册表/WinHTTP/httpx 代理表均为空）、
  客户端库差异（curl 与 httpx 交替 A/B 完全相同 URL 时均为 200）。
- 🟡 未解释：406 的确切触发条件与配额。因此 §7.7 A3 的验收口径需从
  「无未处理 429/503」扩展为「无未处理 429/503/406」。
- ⚠️ **未决风险**：若 406 在真实负载（一次 2000 条 / 10 页抓取）下稳定出现，
  主查询路径将不可用，需要在 T3 用真实负载重新验证并准备缓解方案。
- 新增 `ErrorCode.ARXIV_NOT_ACCEPTABLE`（34 个错误码）。
- 探测工具已加固：默认间隔 4.5 秒（`--interval` 可调）、406 额外退避 30 秒并记录
  `throttle_hits`、退出码 2 表示「本轮被限流，结论不可信」。

**T0.2 已知未测项**：有效偏移窗口 10000 边界、单次切片上限、`max_results=0`、
POST 等价性、大小写、中文关键词、特殊字面量、官网 ±5% 对照。
原因：这些都需要连续多次请求，而 406 会在约 7 次新查询后复发；
继续试探只会加重限流，已主动停止。复测要求写在 `docs/api-verification.md` §4。

**T0.3 分类目录**
- 新增 `scripts/update_categories.py`：抓取官方
  [Category Taxonomy](https://arxiv.org/category_taxonomy) 与
  [canonical Taxonomy.json](https://github.com/arXiv/arxiv-canonical)，
  记录 URL / SHA256 / 抓取时间 / git commit；用 stdlib `HTMLParser` 解析
  Group → Archive → Category 三级结构；
- 未经 `--approve` 绝不覆盖正式文件；`--check` 供 CI 做漂移检测；
  缺中文名时拒绝提升；
- 新增 `scripts/validate_categories.py`：校验 ID 唯一、父子引用、别名无环无多级链、
  active 与别名一致、中文名完整、排序稳定、展开后的查询编码长度；
- 新增 `scripts/data/translations.zh-CN.json`：人工核对的中文名种子
  （8 个 group、20 个 archive、155 个分类）。

**T0.4 工程骨架**
- 建立 src layout：`src/arxiv_filter/`，`pyproject.toml`（setuptools + src layout，
  版本唯一来源为 `arxiv_filter.__version__`）；
- 新增 `src/arxiv_filter/paths.py`：区分只读资源与可写数据目录，
  支持 frozen（`sys._MEIPASS`）与源码两种模式，实现便携模式判定与拒绝理由；
- 新增 `src/arxiv_filter/errors.py`：39 个稳定错误码 + 中英文案 + HTTP 状态 +
  可重试标记 + 统一错误信封渲染；
- 新增 `src/arxiv_filter/__main__.py`（freeze_support → launcher）与
  `src/arxiv_filter/launcher.py`（命令行契约；`--version` 可用，服务端实现属 T4.3）；
- `tests/conftest.py` 实现方案 §7.1 的 socket deny-list 与逐测试临时数据目录，
  live 测试需 `ARXIV_LIVE_TESTS=1`；
- 新增 `start.bat`（发布包入口）与 `start-dev.bat`（源码模式自动找解释器）；
- 依赖以 `requirements.in` / `requirements-dev.in` 声明区间，精确版本与 hash 由
  `requirements.lock` 在构建日锁定。

### 阶段 1 · 纯逻辑核心（T1.1–T1.5）

全部离线可测；434 个测试通过，分支覆盖率 96%。

**T1.1 数据模型**（`models/base.py`、`paper.py`、`query.py`、`job.py`）
- `Author` / `Paper` / `PaperView` / `SearchRequest` / `BuiltQuery` / `FeedPage`，
  以及关键词 AST（`Word`/`Phrase`/`Not`/`And`/`Or`）；
- 时间类型 `UtcDatetime`：**拒绝 naive datetime**（否则本机时区会被静默当成 UTC），
  JSON 序列化固定为 RFC 3339 `Z`，数据库中仍存 `datetime` 对象；
- 领域模型 `extra="forbid"`：字段拼写错误立刻报错，不静默丢掉 `metadata_json` 的字段；
- `Paper` 不变式：`abs_url` 只接受 http/https（挡掉 `javascript:`）、
  分类稳定去重、标题折叠空白、摘要保留内部 LaTeX 与换行；
- `SearchJob` 固化方案 §4.7 的状态机，含 `ALLOWED_TRANSITIONS` 与导出规则
  （`failed` 不可导出，`cancelled` 需用户确认）。

**分类运行时加载**（`taxonomy.py`）
- `load_taxonomy()` 带缓存；加载即校验（schema 版本、ID 唯一、父子引用、
  别名目标存在且**不得多级链**），损坏时抛 `TAXONOMY_CORRUPT` 而不是联网抓页面；
- `resolve_categories()` 实现 D7：`category_id=None` 展开为该一级分类的全部叶子；
- 一个关键语义：`cs` 组的 `cs.SY` 是 `eess.SY` 的别名，因此「全部 cs 子类」
  会包含 `eess.SY`，但**绝不**把 `cs.SY` 本身发出去。

**T1.2 受限 lexer/parser**（`arxiv/query_lexer.py`）
- 手写词法 + 递归下降，实现方案 §4.3 的文法（`OR` 优先级低于 `AND`，支持隐式 AND）；
- **安全边界**：本地拒绝 `: [ ] \`（字段前缀与日期区间注入）、控制字符、
  未闭引号、括号深度 > 8、token > 50、输入 > 500 字符；
- NFKC 是**逐个 token** 做的，因此报错位置始终指向用户看到的原始字符；
  归一化之后**再查一次**禁用字符——全角 `：`(U+FF1A) 会被折成 `:`，
  只查原始输入会漏掉这条注入路径；
- 控制字符**报错而不是静默删除**：悄悄改掉用户要搜的词比给出明确错误更糟
  （方案 §4.3 第 1 条的「去控制字符」保留给 NFKC 与外围空白处理）；
- 错误消息带字符位置、片段与 `^` 指针。

**T1.3 查询构造**（`arxiv/query_builder.py`）
- 分类、日期、关键词分别构造后一次性渲染；基础条件整体加括号避免优先级歧义；
- 7 个 golden query 覆盖方案 §5.2 的映射表，四种 scope、三种 sort；
- **NOT 的语义边界**是本模块最容易写错的地方：`NOT` 只能编译成二元 `ANDNOT`，
  所以 `NOT a AND NOT b` 必须让基础条件充当左侧
  （`(base) ANDNOT all:a ANDNOT all:b`）。若错误地渲染成 `all:a ANDNOT all:b`，
  语义会完全反转成「含 a 但不含 b」；
- `a OR NOT b` 无法在不引入全集的情况下表达，明确拒绝并给出改写建议；
- cache key 严格按 `docs/data-schema.md` §4 构造：`read_filter` / `allow_stale_cache`
  等展示层参数**不进** key，`a b` 与 `a AND b` 命中同一缓存；
- 编码后超过 1800 字符时拆成多个顺序 GET，每片都在预算内。

**T1.4 Atom 解析**（`arxiv/atom_parser.py`）
- 显式命名空间 + `defusedxml`；`normal.xml` 是 2026-09-24 抓取的**原始响应字节**，
  SHA256 记录在 `normal.meta.json`；
- 另外 6 个派生的 fixture：`empty` / `optional-fields` / `cross-list` /
  `legacy-id` / `api-error` / `malformed` / `entity-attack`；
- `abs_url`/`pdf_url` 只从 `entry/link` 的 `rel`/`type`/`title` 取，
  不用字符串替换盲造 URL；DOI 链接与 PDF 链接同为 `rel="related"`，
  靠 `type="application/pdf"` 与 `title` 区分；
- 上游 Atom `Error` entry 与正常论文 entry 分开识别，错误里带上游说明的安全纯文本；
- ACM/MSC 等非 arXiv 分类进 `external_categories`，别名（如 `cs.SY`）仍算 arXiv 分类；
- 缺 `primary_category` 时记警告并留 `None`，**不凭第一个 category 猜测**。

**T1.5 ID 与去重**（`arxiv/ids.py`、`dedupe.py`、`ranking.py`）
- ID 归一化：URL/`arXiv:` 前缀、`.pdf` 后缀、query、fragment 全部剥离；
  只从末尾识别 `v[1-9][0-9]*`（`v0` 不是版本）；旧式 `hep-th/9901001` 保留斜杠；
- 去重：版本号优先 → `updated_at` → 未知版本视为 0；分类取稳定并集；
  主分类冲突写警告；cross-list 只产生一条记录；保持首次出现顺序（不偷偷重排）；
- 本地相关度严格实现方案 §4.4 的公式，负词先排除，权重可配置且进 cache key。

### 版本控制

- 建立本地 Git 仓库（`main` 分支），首次提交含阶段 0–1 全部产物。
- 新增 `.gitattributes`：默认 `* text=auto eol=lf`，并把
  `tests/fixtures/**` 与 `*.lock` 标为 `-text`。
  原因：`tests/fixtures/atom/normal.xml` 的**字节**被 `normal.meta.json` 记录
  并由测试断言，若 Git 在检出时做 LF→CRLF 转换，任何人克隆后跑测试都会失败。
  已用真实克隆验证字节哈希不变。
- 修正 `.gitignore` 的 `data/` 规则：它原本匹配**任意层级**的 `data` 目录，
  连带忽略了 `src/arxiv_filter/data/` 与 `scripts/data/`；而 Git 无法重新包含
  已被忽略目录里的文件，导致随包发布的分类快照与中文名种子文件提交不上。
  已改为锚定仓库根的 `/data/`、`/logs/`、`/exports/`。

### 阶段 3（客户端部分）· 网络与搜索编排（T3.1–T3.3）

离线可测；560 个测试通过，分支覆盖率 95%。T3.4（SearchService）依赖阶段 2 缓存，未做。

**T3.1 限速器**（`arxiv/rate_limiter.py`）
- 全进程唯一、并发串行化；`acquire()` 保证相邻两次发送起点差 **≥ `min_interval_s`**（严格成立）。
- **默认 4.0 秒**，不是方案的 3.1 秒——实测 3.1 秒会被上游以 406 限流。
- 拒绝低于官方下限 3.0 秒的配置，防止被「调优」成违规值。
- 用 **deadline 循环**而不是单次 sleep：Windows 上 `asyncio.sleep` 会提前返回，
  实测 3.1 秒目标只跑出 3.093 秒。退出判据用 1 微秒容差，并把这个容差预先加进
  deadline，使「间隔 ≥ min_interval」严格成立——否则浮点累加会让剩余量停在
  ~1e-13 而永不归零，循环耗尽上界后误报「时钟异常」。
- 等待期间可取消，按 50 ms 分片轮询，满足方案 §4.2 的「100 ms 内结束」。

**T3.2 客户端**（`arxiv/client.py`、`redaction.py`）
- 单连接（`max_connections=1`、`max_keepalive_connections=1`，经 `connection_limits` 可断言）；
  分项超时 10/30/10/10 秒；20 MiB 有界读取。
- 重试：退避 3.2 / 6.4 / 12.8 秒 + 0–1 秒抖动，初次 + 最多 3 次；
  400/401/403/404/410/422 不重试；429 遵守合法 `Retry-After`
  （HTTP-date、负数、超过 600 秒一律视为不可信并回退默认值）。
- **406 按限流处理**：与 429 同路径，退避 30 秒、只重试 1 次、然后 5 分钟熔断。
  绝不归类为查询语法错误——否则用户会以为是自己关键词写错了。
- **先看内容再看状态码**：XML 正文先检测是不是 Atom `error` entry
  （500 也可能是它，那就不该重试）；非 XML 才按状态码分类。
- 重定向只跟随**同源 https**，跨站或降级到 http 一律拒绝；每一跳都占一个限速名额。
- 诊断脱敏：URL 凭据与 `Authorization`/`api_key`/`token`/`password` 等**连值一起**遮蔽，
  正文前缀限 1 KiB、去控制字符、折叠空白。
  早期实现只遮蔽键名、把值留在正文里，被单测当场抓到。
- 新增 `redaction.py` 收拢脱敏逻辑，覆盖率 100%。

**T3.3 分页器**（`arxiv/pagination.py`）
- 禁止 count-only；首包同时拿到论文与 `totalResults`；`target = min(total, cap)`。
- 停滞检测三件套：空页但仍未达 target、`startIndex` 回退、连续两页 ID 集合相同。
- **安全窗口在发请求之前检查**（`start + max_results > 9500` 就停）——
  放在请求之后检查等于已经发出了越界请求。
- 超窗口时按 UTC 分钟递归二分日期。**分片用分钟精度表达**
  （`build_query(request, start_at=..., end_at=...)`）；若沿用「按日期」表达，
  半天片段会被悄悄扩回整天、又超过窗口，二分就白做了。
- 分片探测有**请求预算**（默认 60 次），用尽时保留剩余片段并写警告。
  从整天切到 1 分钟约需 2048 次探测，所以**预算总是先于 1 分钟下限生效**，
  1 分钟那条规则是理论兜底——已在代码与文档中如实说明。
- 取消后不再发下一页。

**T3.3 真实负载验证（406 风险）**

完整结论见 `docs/api-verification.md` §3.6–§3.9：

- ✅ 错误处理正确：406 → `ARXIV_NOT_ACCEPTABLE`、`retryable`、`interpreted_as=rate_limit`；
  只发 2 次请求就熔断；用户可见文案不含「406」也不含「关键词」。
- ❌ **主查询路径当前不可用**：目标场景（cs.CL + 最近 7 天）零结果；
  同日 `id_list` 单条连通性检查也返回 406，而它此前一直稳定 200。
- ⚠️ 恢复时间下界 **≥1 小时**，远长于此前估计；本会话约 100 次探测请求
  **可能加重了限流**，已在文档中如实登记。
- 新增 `tests/live/test_live_arxiv.py`（默认跳过，需 `ARXIV_LIVE_TESTS=1`），
  把这次验证固化成可复跑的测试，避免下次再凭手工命令复现。

### 环境说明

- 开发环境使用 miniforge 的 `arxiv-filter` conda 环境（Python 3.12.x x64，
  conda-forge）。方案 §8.1 中 `.venv\Scripts\python.exe` 的写法在本机等价于
  `D:\miniforge3\envs\arxiv-filter\python.exe`。
- `tests/conftest.py` 的离线 deny-list 只拦**出网**连接并放行回环：
  Windows 上 `asyncio` 用回环 socket 实现 self-pipe，粗暴封掉 `socket.socket`
  会让所有异步测试直接报错。
