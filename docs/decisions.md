# arxiv-filter 决策登记表（T0.1）

> 状态：**已冻结（FROZEN）**
> 冻结日期：2026-09-22
> 依据：`D:\Documents\ChatGPT\arXiv Filter\arxiv-filter-technical-plan.md` 第 10 部分「需要用户确认的问题」
> （该方案文档在项目目录之外，不随仓库分发）
> 适用范围：阶段 0–7 全部实现。任何变更必须在本文件末尾「变更记录」追加一行，不得就地改写历史条目。

本文件是方案第 10 部分 7 个问题的唯一权威答案。`docs/data-schema.md`、`config.py` 默认值、
API 契约与前端行为都从本文件派生；出现冲突时以本文件为准。

---

## D1 · 交互形态：双击后自动打开默认浏览器

| 项目 | 值 |
| --- | --- |
| 决策 | **接受**：双击 exe 后由 Launcher 自动打开默认浏览器访问 `http://127.0.0.1:<随机端口>` |
| 唯一默认值 | `ui.mode = "browser"`；仅监听 `127.0.0.1`，端口由 OS 分配（`bind(port=0)`） |
| 理由 | 浏览器原生日期控件、长列表滚动、可访问性、下载和 KaTeX 能力直接对应需求；FastAPI 把抓取逻辑与 UI 隔离；服务仅监听本机并带启动 token |
| 被否方案 | 原生 GUI（PySide6 体积/插件收集/公式渲染成本高；Tkinter 难达草图交互质量）；CLI（无法提供实时进度、取消、已读状态） |
| 验收关联 | A1（双击到可用 ≤10 秒）、T4.3（LAN 不可访问、无 token 写 API=403） |
| 影响面 | `launcher.py`、`app.py`、`api/*`、`start.bat` |

## D2 · 「新增」与「已读」语义分离

| 项目 | 值 |
| --- | --- |
| 决策 | **分开**：`seen_at` = 曾展示；`read_at` = 用户主动标记或打开 |
| 唯一默认值 | 卡片首次进入 viewport 时按 50 个 ID 批量 `mark_seen`；**不**因滚动写 `read_at` |
| 附加默认值 | `ui.open_marks_read = false`（可选「打开即读」开关，默认关闭） |
| 理由 | 避免滚动一下就丢失未读；`is_new` 按 job 开始时的快照计算，用户滚动时当前列表不跳变 |
| 数据约束 | `mark_seen` 绝不覆盖已存在的 `read_at`；撤销已读只清 `read_at`，不清 `seen_at` |
| 验收关联 | T2.3（seen 不覆盖 read_at）、T5.3、E2E「IntersectionObserver 触发 seen，但本次 is_new 不跳变」 |
| 影响面 | `storage/read_repo.py`、`models/paper.py`（`PaperView`）、`routes_state.py`、`results.js` |

## D3 · 结果超过 2000 条的处理

| 项目 | 值 |
| --- | --- |
| 决策 | **停在 2000 条并提示「继续获取 / 缩小条件」** |
| 唯一默认值 | `result_cap = 2000`（`ge=1, le=2000`）；单次「继续获取」服务端批准目标 `+= 2000` |
| 分页默认值 | 每页 200；上游偏移安全窗口 9500（`start + max_results <= 9500`） |
| 超窗口处理 | 按 UTC 分钟递归二分日期分片，每段 `total <= 9500`；同一分钟仍超限则停止并提示缩小分类或关键词 |
| 理由 | 既保护官方服务又给用户控制；继续时仍串行、限速，并在 9500 窗口前做日期分片 |
| 前端约束 | 前端不得任意传巨大 `additional_results`；进度在合并分片时写「预计」 |
| 验收关联 | A2（指定场景 ≤60 秒、数量 ±5%）、T3.3（9500 窗口分片） |
| 影响面 | `models/query.py`、`arxiv/client.py`、`job_manager.py`、`routes_search.py` |

## D4 · onedir 与 onefile 的交付优先级

| 项目 | 值 |
| --- | --- |
| 决策 | **同时交付**；`onedir` 作为 ≤10 秒冷启动验收基线，`onefile` 作为便利版 |
| 唯一默认值 | 发布页推荐 `arxiv-filter-windows-x64\`（onedir）；两者均由 `packaging/build.ps1 -Mode All` 产出 |
| 判定规则 | onedir 冷启动 P95 ≤10 s 为硬门槛；onefile 若同样达标则两者并列推荐，否则明示「推荐稳定版 onedir」 |
| 附加默认值 | 不启用 UPX；保留 SHA256 与可复现构建记录；不指导用户关闭杀毒软件 |
| 理由 | 单文件解压 + Defender 扫描可能超过 10 秒；两种偏好都要覆盖 |
| 验收关联 | A1、T7.4 |
| 影响面 | `packaging/*.spec`、`packaging/build.ps1`、`docs/release-checklist.md`、`start.bat` |

## D5 · 主 API 不可用时是否自动切第三方

| 项目 | 值 |
| --- | --- |
| 决策 | **不自动切换** |
| 唯一默认值 | MVP 唯一可自动提示的降级是 **stale cache**（需用户确认后展示） |
| 固定降级顺序 | fresh cache → 主 API → transient retry → stale cache →（增强）已预热 OAI 本地索引 →（用户显式启用）第三方 |
| 硬约束 | 第三方结果不得静默计入官方 `totalResults`；不得参与官网 ±5% 验收；主 API 恢复后用 arXiv ID 重新水合官方字段 |
| 理由 | 自动混用会破坏分类、日期和数量语义 |
| 验收关联 | A4（断网）、T3.4（stale 必须确认） |
| 影响面 | `arxiv/provider.py`、`search_service.py`、`api/schemas.py`、前端来源徽标 |

## D6 · KaTeX 是否纳入 MVP

| 项目 | 值 |
| --- | --- |
| 决策 | **不阻塞 MVP**；KaTeX 作为增强项（T5.5），首版默认关闭 |
| 唯一默认值 | `ui.render_latex = false`；`summary_raw` 始终保留原始 LaTeX 文本 |
| 启用后约束 | 本地 vendored 资产、`trust: false`、CSP 不放行内联脚本、渲染失败回退纯文本 |
| 理由 | 避免安全和包体问题拖慢核心交付；原始 LaTeX 始终可读可导出 |
| 裁剪说明 | T5.5 属可裁剪任务，裁剪不影响阶段 0–7 其余验收 |
| 影响面 | `static/js/latex.js`、`static/vendor/katex/`、CSP 配置、导出（始终导出原文） |

## D7 · MVP 是否支持多个二级分类

| 项目 | 值 |
| --- | --- |
| 决策 | **UI 保持单选**；后端与测试从第一天支持 category list |
| 唯一默认值 | `SearchRequest.category_id: str \| None`（`None` = 该一级分类全部叶子分类）；`BuiltQuery.category_ids: list[str]` |
| 复用说明 | 「全部子类」与「全部 <Group>」本地虚拟项同样展开为精确 category OR，复用多分类代码路径 |
| 理由 | 保持原草图单选，界面简单；后端多分类能力供测试与后续增强 |
| 附加默认值 | 不假定 `cat:cs.*` 之类未被官方文档确认的通配语法 |
| 验收关联 | T0.3（taxonomy 展开）、T1.3（7 个 golden query 含多分类） |
| 影响面 | `models/query.py`、`arxiv/query_builder.py`、`data/categories.zh-CN.json`、`query-form.js` |

---

## 与 7 项决策绑定的产品常量（不是 arXiv 官方限额）

这些数值由方案第 2–5 部分给出，作为实现默认值一并冻结。它们**是客户端工程参数**，
不得在文档或 UI 中表述为 arXiv 官方限制。

| 常量 | 冻结值 | 出处 |
| --- | --- | --- |
| 请求起点最小间隔 | **4.0 s**（全进程唯一 limiter；方案原值 3.1 s 经实测不足，见下） | 方案 §5.1、§5.4 + 实测修正 |
| 连接池 | `max_connections=1`、`max_keepalive_connections=1` | 方案 §5.4 |
| 限流响应码 | 429 **和 406**（406 空响应体是实测的限流信号） | 实测，见 `docs/api-verification.md` §3 |
| 每页大小 | 200 | 方案 §5.4 |
| 上游偏移安全窗口 | 9500（探测器测量的当前边界为 10000） | 方案 §5.1、§5.5 |
| 超时 | connect 10 s / read 30 s / write 10 s / pool 10 s | 方案 §4.8 |
| 单 job 上限 | 10 分钟 | 方案 §4.8 |
| 重试 | 初次 + 最多 3 次；退避 3.2 / 6.4 / 12.8 s + 0–1 s 抖动 | 方案 §5.4 |
| 429 处理 | 优先合法 `Retry-After`；否则等 30 s，只重试 1 次，然后 5 分钟熔断 | 方案 §5.4 |
| 日期语义 | UTC 自然日闭区间：起始 `00:00`，截止 `23:59` | 方案「已锁定假设」§3 |
| fresh TTL | 6 小时 | 方案 §4.5 |
| stale 保留 | 30 天 | 方案 §4.5 |
| 缓存上限 | 500 MiB（按 `last_accessed_at` LRU；绝不清理 `read_state`） | 方案 §4.5 |
| 历史保留 | 500 条或 90 天，先到者为准 | 方案 §4.5 |
| 页面心跳 | 15 s | 方案 §1.3 |
| 空闲退出 | 30 分钟无页面且无活动任务 | 方案 §1.3 |
| 任务进度轮询 | 500 ms | 方案 §0.2 |
| 结果列表分页 | 每页 50 条 | 方案 §7.6 |
| results API 单页上限 | `limit <= 100` | 方案 §4.6 |
| 默认日期范围 | 最近 7 天（不自动发请求） | 方案 §8.5 |
| 数据目录 | `%LOCALAPPDATA%\ArxivFilter` | 方案 §3.1 |
| 许可证致谢文案 | `Thank you to arXiv for use of its open access interoperability.` | 方案 §5.1 |

---

## 未决 / 后续仍需用户拍板的事项

以下问题**不阻塞阶段 0**，但在对应阶段开工前必须登记结论：

| 编号 | 问题 | 触发阶段 | 方案建议 |
| --- | --- | --- | --- |
| O1 | 是否接受未来可能需要申请免费 API key | T3（provider 适配器） | 接受，但绝不把 key 写日志 |
| O2 | 默认每次先取 2000 条是否可接受 | 已由 D3 冻结 | — |
| O3 | 是否确认不允许以多 IP / 第三方源绕过限流 | T3 | 确认不允许 |
| O4 | 60 秒目标只约束指定验收场景 | 已由 D3 冻结 | — |
| O5 | 企业 / 校园自定义 CA 是否进 MVP | T3、T5.4 | 增强项，不进 MVP |
| O6 | taxonomy 更新是否随应用版本发布 | T0.3 | 是 |
| O7 | 是否默认保留 cross-list 结果 | 已由方案 §5.1 冻结为保留 | — |
| O8 | 是否引入系统托盘 | T5 | MVP 不引入 |
| O9 | 是否允许安全导出改变危险首字符的显示 | T6.1 | 允许（前置单引号） |
| O10 | 完整关键词是否写磁盘日志 | T4.2 | 否：磁盘默认只记 query hash |
| O11 | 公开发行时的产品显示名 | T7.5 | 需人工确认，避免暗示官方关系 |

---

## 变更记录

| 日期 | 条目 | 变更前 | 变更后 | 原因 |
| --- | --- | --- | --- | --- |
| 2026-09-22 | D1–D7 | （未冻结） | 全部采用方案第 10 部分「推荐」值 | T0.1 首次冻结开工决策 |
| 2026-09-24 | 请求起点最小间隔 | 3.1 s | **4.0 s** | T0.2 实测：3.1 s 间隔下 `search_query` 被上游以 HTTP 406 空响应体拒绝；见 `docs/api-verification.md` §3 |
| 2026-09-24 | 限流响应码 | 只处理 429 | **429 与 406 都按可重试限流处理** | 实测 406 是 arXiv 的限流表达方式，方案 §4.8/§5.4 未覆盖 |
