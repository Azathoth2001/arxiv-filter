# Windows 交付验收清单

> 来源：方案 §7.6（性能预算）、§7.7（A1–A6 手工步骤）、§8.3（构建）、§8.7（迁移）。
> 用法：每个候选包都从「阶段门禁」往下逐项勾选；**未通过项必须写明原因**，
> 不允许以「目前没遇到」作为关闭依据。
>
> 当前状态：阶段 0–1 已完成，阶段 2–7 未开始。因此本清单中大部分项目
> **暂时不可执行**，先按阶段拆好，避免到发布前才发现验收口径。

---

## 阶段门禁

| 门禁 | 要求 | 状态 |
| --- | --- | --- |
| G0（阶段 0 出口） | 7 项决策冻结；API 契约探测有记录；taxonomy 生成+校验通过；干净环境可 hash 安装 | ✅ 通过（2026-09-24） |
| G1（阶段 1 出口） | 纯逻辑核心离线全绿；覆盖核心模块 ≥90%；无联网测试 | ✅ 通过（2026-09-24） |
| G2（阶段 2 出口） | 空库→v1；迁移失败原库 hash 不变；并发写无 lost update | ⬜ 未开始 |
| G3（阶段 3 出口） | 限速器间隔可断言；重试/退避/取消有测试；**406 限流在真实负载下复测** | ⬜ 未开始 |
| G4（阶段 4 出口） | 二次双击不建第二 server；LAN 不可访问；无 token 写 API=403 | ⬜ 未开始 |
| G5（阶段 5 出口） | 五态 UI E2E；XSS fixture 只显示文本 | ⬜ 未开始 |
| G6（阶段 6 出口） | 五种格式 MIME/扩展名/文件名正确；公式注入防护 | ⬜ 未开始 |
| G7（发布） | 本清单全部通过 + SHA256 + release-report.json | ⬜ 未开始 |

---

## A1 · 双击到可用 ≤10 秒

环境：无 Python / Node 的干净 Windows 11 x64 VM，Defender 开启。

- [ ] 清空该版本 LocalAppData 测试目录，重启 VM
- [ ] 双击 onedir exe，计时到页面分类可点、`/health` = ok
- [ ] 重复冷启动 10 次，记录 P50 / P95 / max
- [ ] **P95 ≤ 10 秒**（D4 的硬门槛）
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

- [ ] 导出包含中文、换行、`$E=mc^2$`、引号、逗号、`=1+1` 开头文本的 fixture
- [ ] 用当前受支持 Excel 打开 CSV / xlsx：无乱码、摘要换行、**危险文本不执行公式**
- [ ] 再由测试代码读回逐字段比较
- [ ] CSV 带 UTF-8 BOM（`utf-8-sig`）

## A6 · 连续点击 10 次

- [ ] 在 500 ms 内点击 10 次筛选
- [ ] 只能创建/复用一个 canonical job
- [ ] 日志中只有一组上游请求
- [ ] 完成后等待 60 秒，task / http connection / 文件句柄回到基线
- [ ] RSS 增长满足性能预算

---

## 性能预算（方案 §7.6）

先在目标 Win10/11 x64、Defender 开启、8 GiB RAM 上**建立基线**，再冻结阈值。

- [ ] onedir 冷启动到 health + 页面可用 P95（10 次） ≤ 10 s
- [ ] 解析 synthetic 2000-entry Atom ≤ 2 s
- [ ] 去重 + 三种排序 2000 条 ≤ 1 s
- [ ] 本地 results API 返回首 50 条 ≤ 200 ms
- [ ] 浏览器渲染首 50 卡片到可交互 ≤ 1 s
- [ ] 导出 2000 行 CSV / JSON / Markdown：每种 ≤ 3 s
- [ ] 导出 2000 行 xlsx ≤ 8 s
- [ ] 10 次同查询完成后 RSS 增长 ≤ 50 MiB 且 job/task 数回到基线
- [ ] 数据库 500 MiB prune：后台 ≤ 5 s，UI 不冻结

阈值失败时**先 profile**；不得以提高 arXiv 并发换性能。

---

## 打包（方案 §8.3）

- [ ] 在 Windows x64 上构建（PyInstaller 不是跨编译器）
- [ ] spec 显式收集：`static/**`、`data/*.json`、`storage/migrations/*.sql`、
      KaTeX 资产与 license（若启用）、icon、版本资源、第三方许可证
- [ ] 可写文件一律去 LocalAppData，**不写 `sys._MEIPASS`**
- [ ] `build.ps1` 固定步骤：Git 状态与 commit → 干净 build venv →
      `--require-hashes` 安装 → unit/integration/E2E → onedir → frozen smoke/冷启动 →
      onefile → Defender 扫描（不启用 UPX）→ SHA256 + 清单 + release-report.json
- [ ] 产物：`arxiv-filter-windows-x64\`（onedir，推荐）、`arxiv-filter.exe`（onefile）、
      `SHA256SUMS.txt`、`THIRD-PARTY-LICENSES.txt`、`release-report.json`
- [ ] 包体预算 40–80 MiB（工程预算，需构建验证）；超预算先查测试依赖、
      Playwright、未使用编码器或重复资产是否误入
- [ ] 未参与开发者仅按 README 完成首次搜索与 CSV/xlsx 导出

---

## 数据迁移（方案 §8.7）

- [ ] 启动前用 `sqlite3.Connection.backup()` 写时间戳备份，校验可重新打开后才迁移
- [ ] 每个 migration 一次事务；成功后才更新 `schema_meta`
- [ ] 失败：回滚、保留原库、只读模式启动、中文提示备份路径，**不自动删库**
- [ ] 保留最近 3 个已验证备份
- [ ] cache schema 不兼容可安全丢弃重建；`read_state` 与 history 不得静默丢失
- [ ] 降级安装旧版本且数据库 schema 较新时，拒绝写入并提示

---

## 合规与发布文案（方案 §5.1、第 9 部分）

- [ ] README 与「关于」页含 `Thank you to arXiv for use of its open access interoperability.`
- [ ] 非官方声明齐备；**不使用**官方 logo 或配色
- [ ] 公开发行前确认产品显示名，避免暗示官方关系
- [ ] 风险清单在发布前重新核对：官方 API Terms、User Manual、taxonomy、
      第三方 provider 文档

---

## 已知风险（发布前必须复查）

| 风险 | 状态 | 复查方式 |
| --- | --- | --- |
| **HTTP 406 限流** | 🔴 未关闭 | T3 用真实负载复测；见 `docs/api-verification.md` §3 与 §3.6 |
| legacy API 被替换 | 🟡 监控 | 核对官方 API 公告；Provider adapter 隔离 |
| 有效偏移窗口（9500） | 🟡 未复测 | 发布前跑 `probe_arxiv_api.py --mode window --interval 8` |
| 单次切片上限 / 官网 ±5% | 🟡 未测 | 见 `docs/api-verification.md` §4 的复测要求 |
| taxonomy 漂移 | 🟡 监控 | `update_categories.py --check` 做 CI 漂移检测 |
| onefile 慢 / 误报 | 🟡 未测 | A1 + Defender 扫描 |

> ⚠️ 复测 406 相关项时**必须**至少冷却 10 分钟、用 `--interval 8`、
> 一次只跑一个模式；出现退出码 2 时结果作废，重新冷却后再跑。
