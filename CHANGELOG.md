# 变更记录

本文件同时记录**应用版本**（SemVer）与**数据迁移**（SQLite `schema_version` 单调递增）。
分类快照另有独立版本号，见 `src/arxiv_filter/data/categories.zh-CN.json` 的 `taxonomy_version`。

## [未发布]

### LaTeX 公式渲染（落地 D6 的 KaTeX 增强项，2026-09-27）

- **问题**：标题与摘要里的 LaTeX 一直原样显示（`results.js` 用 `textContent` 写入），`\begin{equation*}…\end{equation*}`、`$u_{t}=Δu^{m}+…$`、`\frac{1}{2}` 等直接以源码示人。这是 T0.1 决策 D6「KaTeX 作为增强项、首版默认关闭」的结果；代码里此前没有任何渲染逻辑。
- **实现**：新增 `static/js/math-render.js`。两处**实测得出的硬约束**决定写法：①KaTeX **不接受带定界符的输入**（传 `$x$` 报 `Can't use function '$' in math mode`，`\begin{equation*}` 报 `can be used only in display mode`），因此必须先分段并剥掉定界符；②渲染走 `katex.render(tex, element)`（该 API 自身即 `textContent=""` + `appendChild`），**不使用 `innerHTML`**，保持既有 XSS 纪律——摘要文本永远没有变成 HTML 的机会。
- **安全**：`trust:false`；并在分段阶段直接拒绝 `\href` / `\url`——实测 `\href{javascript:…}` 在 `trust:false` 下仍会产出该属性。单条公式解析失败只回退该条为原文；KaTeX 缺失时整体回退纯文本（渲染是增强，不是依赖）。
- **资源**：KaTeX 0.16.11 本地化到 `static/vendor/katex/`（js 269 KB + css 21 KB + 仅 woff2 字体 254 KB + MIT LICENSE，共约 547 KB）。新增 `packaging/trim_katex_css.py` 剔掉 CSS 里的 woff/ttf 引用，避免每次加载 40 个 404。**打包无需改 spec**：`collect_runtime_data()` 递归收集 `static/`。
- **CSP 无需改动**：曾预判 `style-src 'self'` 会拦截 KaTeX 的内联 `style=` 属性（其定位大量依赖内联样式）。用 Playwright 实测**否掉了这个预判**——当前 CSP 与显式加 `style-src-attr 'unsafe-inline'` 两种情况下公式节点高度完全一致（14.0781px）、均无 CSP 违规。故**未放宽任何策略**。
- **顺带**：补 `static/favicon.svg` 并在 `index.html` 声明。此前浏览器自动请求 `/favicon.ico` 得到 404，是控制台里唯一的报错噪音。
- **验收（对冻结 exe，不是源码）**：两个包各跑通真实 arXiv 检索，并用**用户报告的那篇** `2609.31393`（Hamilton's gradient estimates…）验证——标题 1 个公式节点、摘要 9 个公式节点 + 2 个块级公式、正文无残留 `$` 与 `\begin{`、控制台 0 报错、公式字体已加载。
- **测试**：新增 11 项前端单测（分段器边界：转义 `\$`、未闭合定界符、`\href` 拒绝、单字符降级、真实摘要形状；渲染器不触碰 `innerHTML`、KaTeX 缺失回退、display 模式）；E2E 新增 1 项（真实浏览器断言 `.katex` 节点 **+ 样式生效 + 公式字体已加载**——只有节点不代表渲染正确）；资源守卫 3 项。离线 763 / E2E 11 / 前端 14 全绿。

### 冻结包依赖与构建脚本修正（2026-09-27）

- **冻结包启动失败（P0）**：本轮首次构建的 exe 双击即崩，报 `ImportError: DLL load failed while importing _ssl`。根因是 **PyInstaller 顺着 PATH 取到了 `D:\PostgreSQL\15\bin` 的 OpenSSL**，而 Conda 把配套的 `libssl-3-x64.dll` / `libcrypto-3-x64.dll` 放在 `Library\bin` —— 该目录不在 PyInstaller 的 import 表搜索路径内。修掉 `_ssl` 后同一成因又暴露为 `_ctypes`（缺 `ffi-8.dll`），因此不再逐个补：`packaging/spec_common.py` 现在用 pefile **读取各 C 扩展的真实导入表**，自动推导出必须随包的 Conda 侧 DLL（当前为 `libssl-3-x64`、`libcrypto-3-x64`、`ffi-8`、`libbz2`、`libexpat`、`liblzma`、`zlib`、`sqlite3`）。`pefile` 已加入 `packaging/requirements-build.in`；`pefile` 不可用时回退到同内容的固定清单。
- **构建脚本末段报错（P0）**：`packaging/build.ps1` 在生成 `release-report.json` 时执行 `PyInstaller --version`，该命令会向 stderr 打印一条 conda 警告；在 `$ErrorActionPreference = 'Stop'` 下 PowerShell 把原生命令的 stderr 提升为终止错误，导致**报告永远生成不出来**、构建以非零码收尾（此前 QA 包缺 `release-report.json` 即此原因）。现仅在报告段放宽偏好，并只取版本行。
- **打包后真实检索验收（此前从未做过）**：直接驱动冻结 exe 走完整链路（health 握手 → 建 job → 轮询 → 取结果），两个模式均在冷查询上取回 **4 篇**（`retries=0`，即未触发 406 重试）：onedir 8.6 MiB、onefile 21.2 MiB。
- **回归防护**：新增 `tests/unit/test_spec_common.py`（7 项，用合成 PE + 真实扩展模块字节校验"只收 Conda 侧副本、不系统 DLL、外来构建会被 verifier 拦下"）。全量离线套件 **767 项通过**；构建内 E2E 7/7、`SHA256SUMS` 109 项。
- **仍未完成**：干净发布 VM、代码签名、签名后 Defender 复扫、目标机 2000 条性能。

### 上游 406 定位与传输层规避（2026-09-27）

- **机制定位**：用对照实验关闭了 `docs/api-verification.md` §3.9 的未决问题。**冷查询**（边缘必然回源）下，同一出口、同一时刻、同一 URL：httpx 得 **406**、curl 得 **200**；**暖缓存**查询下两个客户端都得 200 且返回同一份缓存响应（`first_id` 相同）。结论：406 由**客户端 TLS 指纹**决定，并与「是否需要回源」叠加；这解释了 §3.2 中「curl 与 httpx 交替 A/B 全部 200」的反例。
- **实现**：新增 `arxiv/curl_transport.py`（curl 异步传输 + 模式选择），`ClientConfig` 增加 `transport` 字段，装配处**默认启用 curl**；curl 不可用时**自动回退 httpx 并记 WARNING**，应用不因此启动失败。`ARXIV_FILTER_TRANSPORT=httpx` 可一键退回旧行为。
- **刻意保留**：4.0 s 限速、406 长退避、5 分钟熔断**全部保留**——它们与传输层正交，且在 curl 也被拒时仍是必需的止损路径。**不得**因为换了传输层就删除 406 处理。
- **测试**：新增 17 项**离线**用例覆盖 curl 传输（用假 curl 可执行文件，不出网）：状态码标记解析、正文与 gzip 兜底、406 空响应体不抛错、Host/Content-Length/Accept-Encoding 不转发、四种模式与非法值报错、curl 缺失回退且留 WARNING。全量离线套件 **760 项通过**。
- **默认路径端到端**：不设任何环境变量时，3/3 全新冷查询成功（`cs.CV` total=8、`math.NA` total=9、`cs.DC` total=1）。
- **边界**：样本是 2 条冷查询 + 1 条暖查询，每条 1 次请求；curl 在这些样本上未被拒，不等于 CDN 永不对 curl 出手。目标机/干净 VM 的真实负载验收与打包后复验仍未完成。

### 发布收尾补充（2026-09-26）

- **应用内关于与声明**：新增关于面板，明确说明本工具非 arXiv 官方产品、查询会发送至公开 API，而历史/缓存/已读状态保存在本机；保留 arXiv 要求的致谢原文。
- **Windows 冻结依赖修正**：Conda Python 重建时 PyInstaller 未自动收集 `sqlite3.dll`，导致 frozen app 启动冒烟超时。两种 spec 现显式收集基环境中的 SQLite DLL；onedir/onefile 冒烟均通过。
- **本轮验证与 QA 包**：739 项离线测试通过、6/6 Playwright E2E 与 3/3 Node 测试通过，2 项 live arXiv 测试未运行；当前 dirty worktree 的 onedir/onefile QA 包已生成，107 项 SHA256 逐项匹配。Defender、代码签名、干净发布 VM 和 arXiv 406 冷却复测仍未通过。

### 阶段 4–7 · 本地应用、前端、导出与离线 QA（2026-09-25）

- **T4.3 启动与安全**：loopback 预绑定、单实例锁、随机端口/令牌、CSP 与 Origin 检查、运行时心跳、优雅关闭和空闲退出；新增实际本地 Uvicorn 生命周期集成测试。全程不需要访问 arXiv。
- **T5.1–T5.4 前端**：完成浅色卡片式中文界面，统一青绿色主色、圆角/留白/控件状态；包含四行搜索表单、UTC 日期快捷范围、进度/取消、旧结果保留、50 条分页、摘要/XSS 安全展示、seen/read、新增/未读筛选、历史、脱敏日志、设置和缓存确认。
- **本地偏好设置**：代理 system/none/custom 与“打开即读”设置落入原子写入的 `config.json`；自定义代理保存后按需重建 HTTP client。接口只回显代理协议、主机和端口，凭据、路径和查询参数不回显；活动查询期间拒绝更改。
- **T6.1–T6.4 五种导出**：CSV（UTF-8 BOM、RFC 4180、公式注入防护）、JSON（schema 1.0、RFC 3339 UTC）、Markdown 卡片、write-only XLSX（冻结首行/筛选/列宽/换行/控制字符清洗）、最小 BibTeX（LaTeX 与 key 冲突处理）；API 支持当前 job 子集和安全文件名。
- **T7.3 浏览器验收**：新增仅使用 fake provider 的 Playwright 五态工作流测试；token 门禁、查询/取消、seen/read、XSS 字符串转义、CSV 下载、stale-cache 明示确认，以及 500 ms 内 10 次相同表单提交只生成一个 job/一组上游请求均通过。2000 条结果下首 50 张卡片从 API 响应到可见为 0.3306 秒（最新构建 5 passed）。测试网络由本地回环 API 承载，不访问 arXiv。
- **T7.4 冻结包**：新增 Windows x64 hash-locked QA build、PyInstaller onedir/onefile spec、版本资源、第三方许可证汇总、SHA256SUMS 与 release-report；本机 Windows 11 x64 上两个模式均构建和冻结烟测通过。103 个产物哈希复核一致；onedir 约 38.1 MiB、onefile 约 19.0 MiB。QA 报告明确标记工作区 dirty、Defender 未扫描、签名未配置。
- **最新回归**：732 个 Python 离线测试通过，2 个 live 测试按默认策略跳过，pytest-cov 语句/分支综合覆盖率 93%；Playwright 浏览器测试 5/5、前端 Node 单测 3/3 通过。CSV 与 XLSX 测试现已逐字段读回比较。
- **T7.5 离线性能预检**：2000 条合成记录的 Atom 解析 0.1064 秒、去重+三种排序 0.0057 秒、预热 Results API 首 50 条 0.0236 秒，五种导出均低于各自预算；516 MiB 临时 SQLite 缓存 prune 用时 0.2989 秒且保留 read_state 论文。onedir 冻结烟测 10 次启动 P50/P95/max 为 2.108/2.149/2.149 秒。均为开发机预检，不代表目标机 GUI 冷启动或性能基线。
- **连续查询内存修正**：10 次各持有 2000 条合成结果的查询原先观察到进程 RSS 增长 64.26 MiB；JobManager 现在只保留最近 5 个终态 job。修正后单轮增长 34.03 MiB（34.41 → 68.43 MiB），记录数为 5、活动 job 与待执行任务归零。另修正了同一单调时钟 tick 下 UUID 随机顺序导致新 job 被误淘汰的竞态；相关用例连续运行 10 次通过，外部 task cancellation 路径也纳入限额。目标 VM A6 仍待复测。
- **未完成项**：T5.5 KaTeX 是可选增强，当前保留纯文本摘要以避免 CDN 与 CSP 放宽；目标 VM 的 2000 条数据性能与 A6、干净 VM GUI 冷启动、Defender/签名、发布机 Excel 复验与未参与开发者验收仍待执行。当前开发机已用 Excel 检查 CSV/xlsx 中文、换行与公式样文本；HTTP 406 恢复仍需按冷却纪律做单请求复测，不得批量探测。

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

### 阶段 3 · 网络与搜索编排（T3.1–T3.4）

离线可测；673 个测试通过，分支覆盖率 95%。本阶段无真实 arXiv 请求。

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

**T3.4 SearchService**（`services/search_service.py`）

- fresh cache 命中直接返回，零 provider 调用；缓存和网络结果都执行 canonical ID 去重，
  保留 cross-list 分类并集。
- 主源失败且找到 stale cache 时，默认只返回确认提示、错误与缓存时间/条数摘要，
  不暴露论文；只有请求显式 `allow_stale_cache=true` 才返回过期论文。
- 上游错误先沿用客户端重试/熔断；406 可触发 stale 提示，但查询语法/契约错误不会被旧结果遮住。
- 取消或失败不调用 `put_partial`，不覆盖任何既有完整缓存；成功抓取后才写 complete 并执行 LRU 清理。
- 缓存读写/清理故障有中文 warning；在线完整结果仍可返回。UTC 分钟分片通过 `build_query`
  的 `start_at` / `end_at` 重建，不替换已渲染的 query 字符串。
- SearchService 集成用例覆盖 fresh hit、stale 确认/接受、406 错误原样保留、cross-list 去重、
  取消、partial、缓存读写故障和日期分片工厂；模块分支模式覆盖率 96%。
- 当前 SQLite 缓存 schema 没有保存原始 `totalResults` / `truncated`；缓存命中时服务将这两项
  留为未知，不编造上游总数。若后续 UI 需要精确恢复该信息，应另加兼容 migration。

### 阶段 4 · Job、API 与启动安全（T4.1–T4.2）

693 个离线测试通过，pytest-cov 语句/分支综合覆盖率 94%；真实 arXiv 测试保持跳过。

**T4.1 JobManager**（`services/job_manager.py`）

- 同一规范 cache key 且 stale-cache 授权一致的并发请求复用同一个 job；`read_filter` 仅影响展示，
  不会拆分同一结果集。不同查询在活动任务期间返回 `JOB_LIMIT_REACHED`（409），不排队也不并发请求上游。
- 取消通过可检查 token 传入 SearchService；排队态取消不会调用 SearchService，运行中取消可打断下层工作；
  重复取消保持 `cancelling` / `cancelled` 状态，已完成任务返回 `JOB_ALREADY_FINISHED`（409）。
- 状态快照包含分页进度与冻结后的最终耗时；非法状态迁移不修改任务，并返回 `JOB_INVALID_TRANSITION`（409）。
- 保留终态领域结果，stale 确认结果可供后续 API 层读取；原始上游错误不会被改写，未预期异常对外折叠为 `INTERNAL_ERROR`。
- 离线测试覆盖 10 个相同请求合并、展示筛选合并、异查询拒绝、stale 授权隔离、排队/运行中取消、
  取消幂等、非法迁移、失败错误回传与终态不可再取消。

**T4.2 版本化 HTTP API**（`api/*.py`）

- 新增 `/api/v1` 路由与 DTO：分类、搜索任务/状态/继续/取消/结果、seen/read、历史、日志、导出和缓存清理；
  `X-App-Token` 由依赖注入统一校验，响应统一携带 `X-Request-ID`。
- 所有领域错误、输入校验错误、404/405 与未预期 500 使用稳定错误信封；未预期异常收敛时也保留 request ID，
  不把 traceback/输入原文放入响应。
- OpenAPI 合同快照锁定 12 个操作、认证头和错误信封；API 测试使用假 provider/exporter 与临时 SQLite，
  覆盖 stale 缓存需显式同意、状态/筛选/分页、取消、导出子集与文件名校验、清缓存活动任务保护及日志递归脱敏。
- 继续查询采用新 child job ID，避免改写已终结任务；导出路由已定义并支持注入 exporter，但默认未接入时明确返回
  `EXPORT_NOT_AVAILABLE`（501），实际格式生成仍属 T6。日志路由可注入日志读取器；当前默认适配器返回空列表。
- T4.2 只交付 API 工厂与契约，不启动 HTTP server；loopback、port 0、Origin/CSP、单实例和优雅关闭留给 T4.3。
- 新增 API 的路由模块覆盖率为 96%；项目整体因新增 API 代码由此前 95% 回落至当前综合 94%，后续 QA 继续补测。

### 阶段 2 · SQLite 存储（T2.1–T2.3）

全程离线；657 个阶段 0–2 测试通过，分支覆盖率 95%。本阶段**未访问 arXiv**
（406 风险的冷却期）。

**T2.1 连接与迁移**（`storage/database.py`、`storage/migrations/001_init.sql`）

- 六张表：`schema_meta` / `papers` / `query_cache` / `query_results` /
  `query_history` / `read_state`，四个索引；时间一律 TEXT + RFC 3339 `Z`。
- 启动 PRAGMA：`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`。
- **单连接 + 进程内 `asyncio.Lock`** 串行化；对外暴露 `read(fn)` / `write(fn)`
  而不是 async 事务上下文——这样事务边界完全在工作线程内，
  **不会跨 await 持有事务**（方案 §4.5 的硬要求）。
- 迁移前 `sqlite3.Connection.backup()` 写时间戳备份并**用 `PRAGMA integrity_check`
  验证可重新打开**；每个 migration 一个事务；成功后才更新 `schema_meta`；
  保留最近 3 个已验证备份。
- 失败时回滚、保留原库、**以只读模式启动**（`open()` 不抛异常，
  失败详情走 `migration_error` 属性）——这是方案 §8.7「保留原库并只读启动」的落地，
  用户仍然能看到已有数据。
- 数据库版本比程序新时拒绝写入，中文提示「比程序支持的…新」。

两处容易踩的坑，都已在代码里写明原因：

1. **不能用 `executescript`**：它在执行前会隐式 `COMMIT` 掉外层事务，
   迁移失败就无法回滚——而那正是要防的事。改为用标准库
   `sqlite3.complete_statement` 逐行识别语句边界，再逐条 `execute`。
2. **迁移失败后不能关连接**：关掉的话「只读启动」就成了空话，
   用户连历史记录都看不到。

**T2.2 查询缓存**（`storage/cache_repo.py`）

- `get_fresh` / `get_stale` / `get_any` / `put_complete` / `put_partial` /
  `prune` / `purge_expired` / `clear`。
- 产品默认值：fresh TTL 6 小时、stale 保留 30 天、上限 500 MiB（按
  `last_accessed_at` LRU 淘汰）。
- **不完整的结果永不命中**：`put_partial` 落盘时标 `complete=0`，
  保留下来仅供诊断，`get_fresh`/`get_stale`/`get_any` 一律不返回它。
- **绝不清理 `read_state`**；清理孤儿论文时也会跳过 `read_state` 引用到的记录，
  否则「已读列表」会只剩一串没有标题的 ID。
- 单条坏 JSON 只跳过那一条，不让整份缓存失效。

**T2.3 已读状态与历史**（`storage/read_repo.py`、`storage/history_repo.py`）

- `mark_seen` 用 `ON CONFLICT DO UPDATE` 只推进 `last_seen_at` / 收敛 `first_seen_at`，
  **结构上不可能覆盖 `read_at`**——决策 D2 的核心不变式。
- 撤销已读只置 `read_at = NULL`，**不删行**，`seen_at` 保留。
- 导出/导入按 `canonical_id` 合并：`first_seen=min`、`last_seen=max`、
  `read_at=max(non-null)`；导入容错跳过坏条目、纠正逆序时间戳、
  校验 `schema_version`。
- 历史保留「500 条或 90 天，先到者为准」；`id` 用 **UUIDv7**（按时间单调递增），
  因此可以直接当分页游标。
- 新增 `identifiers.py`：RFC 9562 的最小 UUIDv7 实现（Python 3.12 标准库还没有
  `uuid7`），以及启动令牌用的 `random_token`。

**顺带清理**：删掉了三个写了但从未被调用的 helper
（`rows_to_dicts` / `copy_to` / `serialise_papers` / `placeholders`）——
它们的「测试」只是自己测自己，属于投机性死代码。

### 环境说明

- 开发环境使用 miniforge 的 `arxiv-filter` conda 环境（Python 3.12.x x64，
  conda-forge）。方案 §8.1 中 `.venv\Scripts\python.exe` 的写法在本机等价于
  `D:\miniforge3\envs\arxiv-filter\python.exe`。
- `tests/conftest.py` 的离线 deny-list 只拦**出网**连接并放行回环：
  Windows 上 `asyncio` 用回环 socket 实现 self-pipe，粗暴封掉 `socket.socket`
  会让所有异步测试直接报错。
