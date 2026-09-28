# Windows 交付验收清单

> 来源：方案 §7.6（性能预算）、§7.7（A1–A6 手工步骤）、§8.3（构建）、§8.7（迁移）。
> 用法：每个候选包都从「阶段门禁」往下逐项勾选；**未通过项必须写明原因**，
> 不允许以「目前没遇到」作为关闭依据。
>
> 当前状态（2026-09-27）：阶段 0–6 的实现已完成。离线套件 **767 项通过**、2 项 live 测试按要求排除；Playwright E2E 7/7 通过（fake provider，无 arXiv 出网）。**本轮新建的 onedir/onefile QA 包位于 `packaging/dist`**：两种冻结版冒烟通过，`release-report.json` 与 109 项 SHA256 已生成并逐项匹配。
>
> 本轮修掉两个 P0：①冻结包因 **PyInstaller 从 PATH 取到 PostgreSQL 的 OpenSSL** 而双击即崩（`DLL load failed while importing _ssl`，随后同因暴露 `_ctypes`）——spec 现按 C 扩展真实导入表推导 Conda 侧 DLL；②`build.ps1` 在报告段因原生命令 stderr 触发 `Stop` 而**永远生成不出 `release-report.json`**。
>
> **打包后真实检索已验收**：直接驱动冻结 exe 完成 health 握手 → 建 job → 轮询 → 取结果，两个模式在同一冷查询上均取回 4 篇、`retries=0`（未触发 406 重试）。
>
> 仍未完成：干净发布 VM、可信代码签名、签名后 Defender 复扫、目标机 2000 条性能与 A6、发布机 Excel 复验。工作区仍是 dirty（本轮源码/文档改动未提交），因此该包是 QA 构建而非正式发布。

---

## 阶段门禁

| 门禁 | 要求 | 状态 |
| --- | --- | --- |
| G0（阶段 0 出口） | 7 项决策冻结；API 契约探测有记录；taxonomy 生成+校验通过；干净环境可 hash 安装 | ✅ 通过（2026-09-24） |
| G1（阶段 1 出口） | 纯逻辑核心离线全绿；覆盖核心模块 ≥90%；无联网测试 | ✅ 通过（2026-09-24） |
| G2（阶段 2 出口） | 空库→v1；迁移失败原库 hash 不变；并发写无 lost update | ✅ 通过（2026-09-24） |
| G3（阶段 3 出口） | 限速器间隔可断言；重试/退避/取消有测试；**406 限流在真实负载下复测** | 🟢 根因定位为客户端 TLS 指纹（§3.10）；默认 curl 传输后 3/3 冷查询成功；离线套件 17 项覆盖 curl 传输 |
| G4（阶段 4 出口） | 二次启动握手、loopback、无 token 写 API=403 | ✅ 自动化本机测试通过；LAN 防火墙仍需发布机确认 |
| G5（阶段 5 出口） | 五态 UI E2E；XSS fixture 只显示文本 | ✅ Playwright 7/7 通过（fake provider、无 arXiv 出网；含关于面板与 2000 条首 50 卡片预检） |
| G6（阶段 6 出口） | 五种格式 MIME/扩展名/文件名正确；公式注入防护 | ✅ 离线格式/API 测试及当前开发机 Excel CSV/xlsx 预检通过；干净发布 VM 重验待执行 |
| G7（发布） | 本清单全部通过 + SHA256 + release-report.json | 🟡 QA 包/报告/109 项哈希已生成并复核，冻结包真实检索通过；因工作区 dirty、干净 VM、可信代码签名、签名后 Defender 复扫与目标机性能仍未通过，不可作为正式发布 |

---

## A1 · 双击到可用 ≤10 秒

环境：无 Python / Node 的干净 Windows 11 x64 VM，Defender 开启。

> 当前阻塞（2026-09-26）：本机不是提升权限的管理员会话，Windows Sandbox 不可用，
> 且未发现已安装的 Hyper-V 管理模块、VMware、VirtualBox、Docker 或 QEMU；需要管理员启用可用 VM 方案，或提供干净 Windows 11 x64 VM。

- [ ] 清空该版本 LocalAppData 测试目录，重启 VM
- [ ] 双击 onedir exe，计时到页面分类可点、`/health` = ok
- [ ] 重复冷启动 10 次，记录 P50 / P95 / max
- [ ] **P95 ≤ 10 秒**（D4 的硬门槛）
- [ ] 多开两个应用标签页，关闭其中一个时服务仍可用；关闭最后一个后服务优雅退出并释放端口
- [ ] onefile 单独记录；不达标则发布页把 onedir 标为推荐稳定版
- [ ] 确认数据只写 `%LOCALAPPDATA%\ArxivFilter`，exe 目录无新增文件

## A2 · 指定查询 ≤60 秒、数量 ±5%

- [ ] 记录 UTC 执行时间、起止日、API URL、官网高级搜索 URL、taxonomy 版本
- [ ] 选择 Computer Science → Computation and Language、最近 7 个 UTC 自然日、
      短语 "large language model"
- [ ] 从点击到 completed 计时，**目标 ≤60 秒**
- [ ] 记录：API total、抓取数、canonical ID 去重数、cross-list 数
- [ ] 官网用尽可能相同字段/日期语义，10 分钟内记录数量与截图
- [ ] 差异 >5% 时先查索引时延、官网时区、短语语义、版本/cross-list，
      **不把第三方结果混入比较**

> ⚠️ 该场景依赖 `search_query`。发布前必须先确认 406 限流不会在这个规模的
> 查询上复发（见下方「已知风险」）。

## A3 · 无未处理的上游错误

- [ ] 跑一次分页场景，导出结构化日志
- [ ] 断言请求起点差 ≥ 4 秒（D-2026-09-24 修正值，**不是**方案原文的 3.1 秒）
- [ ] 断言最大的并发 connection 数为 1
- [ ] 断言最小起点间隔实测值 ≥ 4.0 秒（`time.sleep` 会提前返回，需 deadline 循环）
- [ ] **无未处理的 429 / 503 / 406**；若自然出现，必须进入
      retry / failed / stale 的已处理状态
- [ ] 没有 traceback 泄漏到 UI

> 方案 §7.7 A3 原文只写「429/503」，2026-09-24 实测后扩展为包含 **406**。

## A4 · 断网

- [ ] 先完成并缓存一次查询
- [ ] 把应用代理临时改为不可达的 `http://127.0.0.1:9`，重复查询
- [ ] 有 fresh：零网络、正常显示
- [ ] 只有 stale：明确显示缓存时间并等用户确认
- [ ] 无 cache：中文提示，程序和旧列表保持
- [ ] 测试后恢复原代理配置

## A5 · Excel / 中文 / 公式

- [x] 导出包含中文、换行、`$E=mc^2$`、引号、逗号、`=1+1` 开头文本及公式样批注的 fixture
- [x] 当前 Windows 11 开发机用 Excel 实际打开 CSV / xlsx：中文与摘要换行正常；CSV 导入向导选择 UTF-8（65001）和逗号分隔；C2 `'=1+1` 与 K2 `'=HYPERLINK(...)` 均显示为文本，没有执行公式
- [x] CSV 与 XLSX 均由测试代码读回，并逐字段比较整行结果
- [x] CSV 带 UTF-8 BOM（`utf-8-sig`）
- [ ] 在干净发布 VM 上重复 Excel 打开检查；本机结果仅为开发机预检

## A6 · 连续点击 10 次

- [ ] 在 500 ms 内点击 10 次筛选
- [ ] 只能创建/复用一个 canonical job
- [ ] 日志中只有一组上游请求
- [x] Playwright 开发机补充检查：500 ms 内触发 10 次相同表单提交，复用一个 canonical job，fake provider 只收到 1 个分页请求；这不替代目标 VM 上的真实 UI 点击验收
- [ ] 完成后等待 60 秒，task / http connection / 文件句柄回到基线
- [ ] RSS 增长满足性能预算

---

## 性能预算（方案 §7.6）

先在目标 Win10/11 x64、Defender 开启、8 GiB RAM 上**建立基线**，再冻结阈值。

### 当前开发机预检（非发布门禁）

2026-09-25 在当前 Windows 11 开发机用 synthetic fixture 做了预检，目的是尽早发现数量级问题，
不是目标 8 GiB 干净 VM 的正式验收；完整 GUI 冷启动、目标机浏览器交互和 500 MiB prune 仍需复测。
Atom、排序和导出均使用 2,000 条合成论文；Results API 使用预热 SQLite cache，走进程内 ASGI，provider 明确禁止调用。

| 项目 | 单轮观测 | 方案预算 | 结论 |
| --- | ---: | ---: | --- |
| Atom 解析 2,000 entries | 0.1064 s | ≤2 s | 开发机预检通过，目标 VM 待复测 |
| 去重 + submitted/updated/relevance 三种排序，2,000 条 | 0.0057 s | ≤1 s | 开发机预检通过，目标 VM 待复测 |
| Results API 首 50 条（总计 2,000 条） | 0.0236 s | ≤200 ms | 开发机预检通过，预热 cache / 进程内 ASGI |
| 浏览器首 50 张卡片（总计 2,000 条） | 0.3306 s | ≤1 s | 本轮 E2E suite 记录；目标 VM 待复测 |
| onedir 冻结烟测启动（10 次、隔离数据目录） | P50 2.108 s；P95 2.149 s；max 2.149 s；每轮验证 health、首页/JS、分类接口及优雅关闭 | P95 ≤10 s | 当前开发机进程/冷数据烟测预检；不等同于目标 VM 的 GUI 可交互冷启动 |
| 连续 10 次 2,000 条合成结果查询 + 首页快照 | RSS 34.41 → 68.43 MiB（+34.03 MiB）；0.7868 s；保留记录 5、活动/待执行任务 0 | 增长 ≤50 MiB | 单轮 JobManager + 合成 SearchService 预检通过；不是完整 UI A6 或目标 VM 验收 |
| CSV / JSON / Markdown / XLSX / BibTeX 导出 2,000 条 | 0.0285 / 0.0265 / 0.0278 / 0.4647 / 0.0212 s | ≤3 / ≤3 / ≤3 / ≤8 / 未设 | 开发机预检通过 |
| SQLite cache prune，2,000 条 / 540,954,892 B（约 516 MiB） | 0.2989 s | ≤5 s | 临时磁盘库；读状态论文保留；目标 VM 待复测 |

以上是开发机预检观测、非正式发布基线；连续查询 RSS 使用假 SearchService 构造合成结果。
启动样本只覆盖冻结应用探针，不覆盖目标机完整桌面交互；目标机性能、A1 冷启动和完整 UI A6 仍需复测。

- [ ] onedir 冷启动到 health + 页面可用 P95（10 次） ≤ 10 s
- [ ] 解析 synthetic 2000-entry Atom ≤ 2 s
- [ ] 去重 + 三种排序 2000 条 ≤ 1 s
- [ ] 本地 results API 返回首 50 条 ≤ 200 ms
- [ ] 浏览器渲染首 50 卡片到可交互 ≤ 1 s
- [ ] 导出 2000 行 CSV / JSON / Markdown：每种 ≤ 3 s
- [ ] 导出 2000 行 xlsx ≤ 8 s
- [ ] 目标 VM 完整应用中 10 次同查询后 RSS 增长 ≤ 50 MiB 且 job/task 数回到基线
- [ ] 数据库 500 MiB prune：后台 ≤ 5 s，UI 不冻结

阈值失败时**先 profile**；不得以提高 arXiv 并发换性能。

---

## 打包（方案 §8.3）

- [x] 已在 Windows 11 x64 本机以干净 Python 3.12 build venv 构建 onedir/onefile；
      最新缓存 schema 修复版位于 `%TEMP%\arxiv-filter-cache-schema-qa-20260926\dist`，工作区含未提交改动，报告标记为 QA 而非正式发布
- [x] spec 收集 `static/**`、`data/*.json`、`storage/migrations/*.sql`、版本资源、运行时 SQLite DLL 与第三方许可证；
      KaTeX 当前禁用，产品 icon 尚未批准/提供
- [x] 最新 onedir/onefile 冻结烟测确认本地 UI/API/分类资源可用，数据写入隔离目录，bundle 目录未被改写
- [x] `build.ps1` 执行 Git 状态/commit 记录、哈希锁安装、unit/integration/E2E、
      onedir/onefile、冻结烟测、SHA256 与 release-report；UPX 关闭
- [x] QA 产物：`onedir\arxiv-filter-windows-x64\`、`onefile\arxiv-filter.exe`、
      `SHA256SUMS.txt`、`THIRD-PARTY-LICENSES.txt`、`release-report.json`；107 项哈希全部匹配
- [x] 最新包体已实测：onedir 41,269,316 B（约 39.4 MiB），onefile 20,665,417 B（约 19.7 MiB）；
      Playwright 与测试框架未进入应用包
- [x] 当前开发机 Defender 自定义扫描隔离 QA 包（2026-09-26；签名版本 1.459.405.0；0 项检出；结果记录在 QA `release-report.json`）
- [ ] 干净发布 VM 冷启动/冷数据检查，并在可信代码签名后重新执行 Defender 扫描
- [ ] 可信代码签名与公开产品 icon；发布文案/非官方声明确认

> 当前签名阻塞（2026-09-26）：当前用户/本机证书库没有带代码签名 EKU 和私钥的证书，系统也未安装 SignTool。需要在本机安全配置受信任的发布证书/签名服务后才能签名；不使用自签名证书代替正式签名。
- [ ] 未参与开发者仅按 README 完成首次搜索与 CSV/xlsx 导出

---

## 数据迁移（方案 §8.7）

- [x] 启动前用 `sqlite3.Connection.backup()` 写时间戳备份，校验可重新打开后才迁移
- [x] 每个 migration 一次事务；成功后才更新 `schema_meta`
- [x] 失败：回滚、保留原库、只读模式启动、中文提示备份路径，**不自动删库**
- [x] 保留最近 3 个已验证备份
- [x] cache schema 不兼容时先验证备份，再仅重建 `papers` / `query_cache` / `query_results`；`read_state` 与 history 保留。重建失败事务回滚并只读启动；耐久表结构不兼容时不自动修复并只读启动
- [x] 降级安装旧版本且数据库 schema 较新时，拒绝写入并提示

---

## 合规与发布文案（方案 §5.1、第 9 部分）

- [x] README 与「关于」页含 `Thank you to arXiv for use of its open access interoperability.`
- [x] 非官方声明齐备；界面使用自绘标记，未使用 arXiv 官方 logo 或配色
- [ ] 公开发行前确认产品显示名，避免暗示官方关系
- [ ] 风险清单在发布前重新核对：官方 API Terms、User Manual、taxonomy、
      第三方 provider 文档

---

## 已知风险（发布前必须复查）

| 风险 | 状态 | 复查方式 |
| --- | --- | --- |
| **HTTP 406 拒绝** | 🟢 **已定位并按传输层规避**；待目标机真实负载复验 | 见下方专项 |
| legacy API 被替换 | 🟡 监控 | 核对官方 API 公告；Provider adapter 隔离 |
| 有效偏移窗口（9500） | 🟡 未复测 | 发布前跑 `probe_arxiv_api.py --mode window --interval 8` |
| 单次切片上限 / 官网 ±5% | 🟡 未测 | 见 `docs/api-verification.md` §4 的复测要求 |
| taxonomy 漂移 | 🟡 监控 | `update_categories.py --check` 做 CI 漂移检测 |
| onefile 慢 / 误报 | 🟡 未测 | A1 + Defender 扫描 |

### 🟢 HTTP 406 专项（已定位；等待目标机复验）

2026-09-24 14:48 UTC 的真实负载验证：目标场景（cs.CL + 最近 7 天 + 短语）
**一个结果都没拿到**；同日 14:47 连 `id_list` 单条连通性检查也返回 406，
而它在 13:19–13:40 期间是稳定 200 的。
完整证据与时间线见 `docs/api-verification.md` §3.6–§3.9。

已经做对的部分（**不要再改坏**）：

- [x] 406 被判为可重试限流（`ARXIV_NOT_ACCEPTABLE`），而不是查询语法错误
- [x] 只重试 1 次就熔断，不连续敲上游
- [x] 用户可见文案不含「406」，也不把责任推给用户的关键词

2026-09-27 补充（`docs/api-verification.md` §3.10）：根因是**客户端 TLS 指纹**，
不是限速参数——同一出口、同一时刻、同一 URL，httpx 得 406 而 curl 得 200；暖缓存下两者均 200。
已实现默认 curl 传输（`arxiv/curl_transport.py`，D8），并有 17 项离线测试覆盖 curl 路径。

尚未完成：

- [x] 定位 406 与「是否需要回源」及客户端指纹的关系（已关闭 §3.9 的机制问题）
- [x] 实现并默认启用 curl 后端传输；保留 406 退避与熔断作为第二道防线
- [ ] **在目标机/干净 VM 上跑一次真实负载验收**（A2/A3），确认 curl 路径同样通过
- [ ] 若 curl 在目标网络也被拒：联系 arXiv 说明用途（个人研究客户端、遵守限速、单连接）
- [ ] 若被拒成为常态：评估 OAI-PMH 预热本地索引作为主路径
      （注意其 datestamp **不是**提交日期，不能直接替代日期筛选）
- [ ] 复验期间遵守复测纪律：一次一个请求，出现 406 立即停止，不做批量探测

> ⚠️ **复测纪律**：一次只发**一个**请求；出现 406 立即停止并延长冷却，
> 不得为了「跑绿」而反复重试。
> A2 与 A3 的解阻条件已从「等上游恢复」改为「目标机复验 curl 传输路径」。

### 阶段 2 交付后新增的可验收项

- [x] 迁移失败后原库 SHA256 不变、部分结果被回滚、以只读启动
- [x] 只读模式下仍能读到已有数据（否则「只读启动」没有意义）
- [x] 10 个并发写无 lost update
- [x] `mark_seen` 不覆盖 `read_at`（结构性保证，非仅测试）
- [x] `read_state` 在任何清缓存/清历史操作后都完好
- [x] 导入/导出往返一致
- [x] 开发机临时 SQLite 以 2,000 条合成记录（540,954,892 B）验证默认 500 MiB prune：0.2989 s；read_state 引用的论文保留
- [ ] 在目标 8 GiB 发布机重复大缓存淘汰与连续查询 RSS 测量
