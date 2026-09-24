# arxiv-filter

Windows 本地 arXiv 文献收件箱：用中文界面组合**分类 + UTC 日期 + 关键词**，
串行、限速地读取 arXiv Atom 元数据，在本机完成去重、排序、缓存、新增/已读追踪和五种格式导出。

无需账号、无需 Docker、无需数据库服务器。数据只写本机 `%LOCALAPPDATA%\ArxivFilter`。

> 当前状态：**阶段 1 已完成**。纯逻辑核心（数据模型、关键词解析、查询构造、
> Atom 解析、ID 与去重）可用并有 434 个离线测试覆盖。
> Web 服务、UI、SQLite 存储、上游客户端、导出器与打包**尚未实现**，
> 因此现在还不能作为完整应用验收。进度对照见下方「实现进度」。

---

## 实现进度

对照方案第 6 部分的阶段划分：

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 0 | 决策与事实验证（T0.1–T0.4） | ✅ 已完成 |
| 1 | 纯逻辑核心（T1.1–T1.5） | ✅ 已完成 |
| 2 | SQLite 存储（T2.1–T2.3） | ⬜ 未开始 |
| 3 | 网络与搜索编排（T3.1–T3.4） | ⬜ 未开始 |
| 4 | Job / API / 启动安全（T4.1–T4.3） | ⬜ 未开始 |
| 5 | 前端（T5.1–T5.5） | ⬜ 未开始 |
| 6 | 导出（T6.1–T6.4） | ⬜ 未开始 |
| 7 | QA、打包与交付（T7.1–T7.5） | ⬜ 未开始 |

现在可以运行的只有开发者命令（见下）；`python -m arxiv_filter` 会明确提示
「Web 服务尚未实现（计划在 T4.3 完成）」，不会假装启动成功。

---

## 这是什么 / 不是什么

**是**：一个个人的、离线优先的文献筛选工具。它解决的是「每天翻 arXiv 新投稿太慢」的问题。

**不是**：不是 arXiv 官方产品，不代表 arXiv 立场。它是把 arXiv 公开的
[legacy Atom API](https://info.arxiv.org/help/api/user-manual.html) 包了一层中文界面和本地缓存。

> Thank you to arXiv for use of its open access interoperability.

## 30 秒开始

```bat
:: 1. 获取源码
git clone <repo> arxiv-filter
cd arxiv-filter

:: 2. 建环境（本机用 miniforge；若已有 Python 3.12 也可用 py -3.12 -m venv .venv）
D:\miniforge3\Scripts\conda.exe create -n arxiv-filter -c conda-forge python=3.12 pip -y

:: 3. 安装
D:\miniforge3\Scripts\conda.exe run -n arxiv-filter python -m pip install -e ".[dev]"

:: 4. 检查
D:\miniforge3\Scripts\conda.exe run -n arxiv-filter python -m arxiv_filter --version
```

源码模式下双击 `start-dev.bat` 即可，它会自动按 `.venv` → miniforge `arxiv-filter` → PATH 的顺序找解释器。

---

## 常用命令

```powershell
# 版本
python -m arxiv_filter --version

# 离线测试（禁止联网，方案 §7.1 用 socket deny-list 强制）
python -m pytest -m 'not live_arxiv' -q

# 校验内置分类快照
python scripts/validate_categories.py

# 对照官方源刷新分类快照（真实联网）
python scripts/update_categories.py            # 生成候选 + 打印 diff
python scripts/update_categories.py --approve  # 人工审核后提升为正式文件

# arXiv API 契约探测（真实联网，自动限速，默认间隔 4.5 秒）
python scripts/probe_arxiv_api.py --mode smoke
python scripts/probe_arxiv_api.py --mode fields --json-out probe-fields.json
```

## 限流与礼貌访问

本产品严格遵守 arXiv 的
[Terms of Use for arXiv APIs](https://info.arxiv.org/help/api/tou.html)：

- 全局**单连接**，任意两次请求起点至少间隔 **4 秒**（官方下限是 3 秒；
  2026-09-24 实测贴着 3 秒会被上游以 HTTP 406 空响应体限流，见
  `docs/api-verification.md`）；
- 单实例运行，相同查询合并为一个上游序列，不做并发补偿；
- 二级失败退避 3.2 / 6.4 / 12.8 秒；429 遵守 `Retry-After`，退避后仍失败则 5 分钟熔断；
- **不**通过切换域名、多个 IP 或第三方镜像规避限流；
- 主源失败时只提示使用过期缓存（需用户确认），**不**自动混用第三方数据。

## 数据与隐私

所有数据都在本机：

```text
%LOCALAPPDATA%\ArxivFilter\
├─ config.json
├─ arxiv-filter.db        # 缓存、历史、已读状态（SQLite + WAL）
├─ runtime.json           # 单实例的 PID/端口/随机挑战
├─ logs\app.log           # 5 × 2 MiB 轮转
├─ exports\               # 默认下载目录
└─ backup\                # 迁移前的数据库备份，保留最近 3 个
```

- 程序**不**向 exe 所在目录写任何文件；
- 磁盘日志默认只记查询哈希，不记完整关键词；代理密码在日志中显示为 `***`；
- 服务只监听 `127.0.0.1`，端口随机，并带每次启动生成的 128-bit 令牌；不开放 CORS。

## 文档

| 文档 | 内容 |
| --- | --- |
| `docs/decisions.md` | T0.1 冻结的 7 项产品决策 + 全部工程常量与变更记录 |
| `docs/data-schema.md` | 时间/UTC 语义、seen-read 状态、SQLite、缓存键、HTTP 契约、taxonomy 运行时契约 |
| `docs/api-verification.md` | 每次实测的日期、命令与原始结论（含 HTTP 406 限流发现） |
| `docs/release-checklist.md` | Windows 交付验收清单（A1–A6）与阶段门禁 |
| `CHANGELOG.md` | 各阶段变更记录 |

## 关于「新增」与「已读」

两者**分开**：论文第一次出现在结果列表里只记「已展示」（`seen_at`），
只有你主动点「标记已读」或（打开开关后）打开 abs/PDF 才记「已读」（`read_at`）。
这样滚动浏览不会把没读的论文标成已读。

## 许可证与致谢

数据来自 arXiv，使用其开放互操作接口。本工具与 arXiv 无隶属关系，
未获得 arXiv 的赞助或背书。
