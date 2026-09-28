# arXiv Filter

[English](README.md) · **简体中文**

> 本地优先的 arXiv 文献收件箱：按分类、UTC 提交日期与关键词筛选，在本机完成
> 阅读、追踪与导出。

<span id="这是什么"></span>

## 这是什么

一个 Windows 本地小工具，把 arXiv 的 Atom 接口变成可筛选的阅读清单。你选定分类、
UTC 日期范围与（可选）关键词，它去抓取匹配的论文，在本机完成去重、缓存、新增/已读
追踪，并可导出为五种格式。

- 无需账号、无需 Docker、无需数据库服务器。
- 数据只写到 `%LOCALAPPDATA%\ArxivFilter`。
- 界面服务只监听 `127.0.0.1`，并带每次启动随机生成的访问令牌。

**它不是** arXiv 官方产品，也不代表 arXiv 的立场。它只是把 arXiv 公开的
[legacy Atom API](https://info.arxiv.org/help/api/user-manual.html) 包了一层中文界面
和本地缓存。

> Thank you to arXiv for use of its open access interoperability.

## 功能

| | |
|---|---|
| **检索** | 一级 + 二级分类、UTC 日期闭区间、关键词表达式（`AND` / `OR` / `NOT` 与引号短语）、搜索范围（全部字段/标题/摘要/作者）、三种排序、可配置结果上限 |
| **阅读清单** | 卡片式展示、就地展开摘要、标记已读/未读、"新增 vs 已见"追踪（滚动时当次列表不跳变） |
| **LaTeX 渲染** | 标题与摘要里的公式渲染为真正的数学排版（KaTeX 本地内置，离线可用） |
| **本地缓存** | SQLite + WAL；新鲜结果复用 6 小时，过期结果保留 30 天，500 MiB LRU 上限且**绝不清理已读状态** |
| **查询历史** | 最近 500 条（或 90 天）；点一下把条件填回表单 |
| **导出** | CSV（Excel 兼容、公式注入防护）、XLSX、JSON、Markdown、BibTeX |
| **隐私** | 仅回环监听、CSP 不放行内联脚本、无第三方统计、诊断日志脱敏、代理凭据不回显 |

## 环境要求

- **Windows 10（1803+）或 Windows 11，64 位**
- 运行现成 exe：不需要其他任何东西
- 从源码运行/构建：**Python 3.12 x64**；前端测试另需 **Node.js 24+**

> 应用默认通过系统自带的 `curl.exe` 访问 arXiv —— 因为 arXiv 的边缘层会间歇性地
> 以 `HTTP 406` 拒绝 `httpx` 的客户端指纹。`curl` 在 Windows 10 1803+ 上是系统组件；
> 若缺失，应用会**自动回退到 httpx 并记一条 WARNING**，此时可能撞上那个 406。
> 详见 `docs/api-verification.md` §3.10。

## 快速开始

### 方式 A — 直接运行现成 exe

下载发布包后双击：

```text
arxiv-filter-windows-x64\start.bat      # 推荐（onedir：启动最快）
arxiv-filter.exe                        # 单文件版，同样自包含
```

界面会自动在默认浏览器里打开。程序不写注册表、不装到系统里，**删掉文件夹就等于卸载**。

首次运行 Windows 可能弹 SmartScreen 提示（未签名），选**更多信息 → 仍要运行**即可。

### 方式 B — 从源码运行

```bat
git clone <你的仓库地址> arxiv-filter
cd arxiv-filter

:: 2. 建环境（必须是 3.12）
conda create -n arxiv-filter -c conda-forge python=3.12 pip -y
conda run -n arxiv-filter python -m pip install -e ".[dev]"

:: 3. 检查
conda run -n arxiv-filter python -m arxiv_filter --version
```

然后双击 `start-dev.bat`：它会按 `.venv` → conda 环境 `arxiv-filter` → `PATH` 的顺序
找解释器，启动本地服务并打开界面。

## 30 秒上手

1. 选**一级分类**（默认「数学」），可选**二级分类**（默认「偏微分方程分析」）。
2. 调整 **UTC 日期范围** —— 默认已填最近 7 天，`3 天 / 7 天 / 30 天 / 本月` 一键切换。
3. 需要缩小范围就填**关键词**；留空则只按分类与日期检索。写法示例：
   - `PDEs AND Hessian AND estimate` —— 三个词都要有（取交集）
   - `PDEs Hessian estimate` —— 与上面完全等价，词与词之间默认就是 AND
   - `"large language model" AND retrieval NOT survey` —— 短语 + 交集 + 排除
   - **不要用逗号分隔**（`PDEs, Hessian` 会被当成含逗号的词，搜不到）
4. 点「**开始筛选**」。进度实时显示，随时可取消。
5. 处理结果：点标题去 arXiv 原文、展开摘要、标记已读/未读、翻页；最后用「导出」把
   **当前筛选条件下的全部结果**导出。

更详细的中文使用说明与常见问题见 [`docs/使用教程.md`](docs/使用教程.md)。

## 工作原理

```text
浏览器界面  ──HTTP──▶  127.0.0.1 上的 FastAPI（令牌门禁 + CSP 锁定）
                            │
                            ├─ SearchService ──▶ SQLite 缓存 ──▶ ArxivClient
                            │                     （新鲜 / 过期 / 未命中）
                            └─ JobManager（单一串行 job、进度、取消）
                                                     │
                                        curl 子进程 ──▶ export.arxiv.org Atom 接口
```

几个值得知道的设计取舍：

- **一次只发一个请求。** 全进程唯一的限速器强制请求起点间隔 ≥4.0 秒、单连接；相同查询
  在并发时合并为一条上游序列。
- **406 当作上游拒绝，而不是用户语法错误。** 它与 `429` 走同一条退避路径（重试 1 次后
  打开 5 分钟熔断），并且**绝不**归因于用户的关键词。
- **不自动切换第三方镜像。** 上游失败时只提供**过期缓存**，且必须用户确认后才展示。
- **原始 LaTeX 始终保留。** 公式渲染只影响 DOM；五种导出格式里的 `summary_raw` 都是原文。

## 对 arXiv 的礼让

本产品严格遵守 arXiv 的 [API Terms of Use](https://info.arxiv.org/help/api/tou.html)：

- 全局**单连接**，任意两次请求起点至少间隔 **4 秒**（官方下限 3 秒；2026-09-24 实测贴着
  3 秒会被上游以 HTTP 406 空响应体拒绝，见 `docs/api-verification.md`）；
- 单实例运行，相同查询合并为一条上游序列，**不做并发补偿**；
- 二级失败退避 3.2 / 6.4 / 12.8 秒；429 遵守 `Retry-After`，退避后仍失败则熔断 5 分钟；
- **不**通过切换域名、多 IP 或第三方镜像规避限流；
- 主源失败时只提示使用过期缓存（需用户确认），**不**自动混用第三方数据。

## 测试

```powershell
# 离线套件（用 socket deny-list 强制禁止联网）
python -m pytest -m 'not live_arxiv' -q

# 浏览器 E2E（需 Chrome/Edge；使用假数据源，不访问 arXiv）
$env:ARXIV_FILTER_BROWSER = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
python -m pytest -m e2e -q

# 前端单测（Node.js 24+）
node --test "tests/frontend/*.test.mjs"

# 真实 arXiv 测试（默认跳过）
$env:ARXIV_LIVE_TESTS = '1'; python -m pytest tests/live -q
```

当前状态：**离线 763 项**、**浏览器 E2E 11 项**、**前端单测 14 项**，全部通过。
覆盖率由 `pytest-cov` 统计；离线套件一旦尝试开 socket 就会失败。

## 构建 Windows 可执行文件

```powershell
powershell -File packaging\build.ps1 -Mode All -Python <python3.12-x64.exe 路径>
```

脚本会建立隔离的、哈希锁定的构建环境，跑完离线 + 浏览器套件，用 PyInstaller 冻结
`onedir` 与 `onefile` 两种形态，执行冻结包冒烟测试，并生成 `SHA256SUMS.txt` 与
`release-report.json`。工作区不干净时它会拒绝构建，除非显式传 `-AllowDirty`
（会被明确标记为 QA 构建，而非正式发布）。

## 目录结构

```text
src/arxiv_filter/
├── arxiv/          客户端、限速器、查询词法/构建、Atom 解析、curl 传输层
├── api/            FastAPI 路由、schema、安全响应头
├── services/       检索编排与 job 管理
├── storage/        SQLite、缓存/历史/已读仓储、迁移
├── static/         界面（原生 ES 模块，无构建步骤）+ 内置 KaTeX
└── data/           分类快照（中文）
docs/               实测记录、决策登记、发布清单、使用教程
packaging/          PyInstaller spec 与构建工具
scripts/            分类维护、API 契约探测、许可证清单
tests/              unit · e2e(Playwright) · frontend(node:test) · live(需显式开启)
```

## 数据与隐私

所有数据都在本机：

```text
%LOCALAPPDATA%\ArxivFilter\
├── config.json          偏好设置（代理模式、打开即读）
├── arxiv-filter.db      缓存、查询历史、已读状态（SQLite + WAL）
├── runtime.json         单实例的 PID / 端口 / 随机挑战
├── backup/              数据库快照
├── exports/             你导出的文件
└── logs/                脱敏日志（关键词在磁盘上只记哈希）
```

唯一的出网流量就是那次 arXiv API 查询本身。没有遥测，没有账号。

## 许可证

MIT —— 见 [`LICENSE`](LICENSE)。

随包分发的第三方组件（KaTeX，以及冻结构建的 Python 依赖）及其许可证列在
`THIRD-PARTY-LICENSES.txt` 中，由 `scripts/write_third_party_licenses.py` 生成。
