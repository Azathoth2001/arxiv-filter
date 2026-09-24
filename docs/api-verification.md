# arXiv API 契约核验记录

> 本文件记录**每一次实测**的日期、命令、原始结果与结论。方案 §5.7 的
> `【待实测验证】` 清单在这里逐条关闭或标记为未决。
>
> 规则：结论必须附命令与原始输出摘要；不得用「目前没遇到」作为关闭依据；
> 与官方文档冲突时，**记录冲突**而不是改写文档。

实测环境（除非另注）：

| 项目 | 值 |
| --- | --- |
| 日期 | 2026-09-24（UTC） |
| 出口 | 直连，无 `HTTP_PROXY`/`HTTPS_PROXY`；WinINET `ProxyEnable=0`；WinHTTP 直连 |
| 客户端 | `httpx 0.28.1`（Python 3.12.14，conda-forge）/ `curl.exe` |
| User-Agent | `arxiv-filter-connectivity-check/0.1 (personal research client)` |
| 端点 | `https://export.arxiv.org/api/query` |

---

## 结论摘要

| 命题 | 结论 | 置信度 |
| --- | --- | --- |
| 端点仍可用，返回 Atom 1.0 + OpenSearch | **成立** | 高 |
| `id_list` 查询 | **成立**，稳定 200 | 高 |
| `search_query` 查询 | **成立**，但存在限流风险，见下 | 高 |
| HTTP → HTTPS 重定向 | **成立**（301，同域） | 高 |
| 单次切片上限 | 未测量（未跑 window 模式） | — |
| 有效偏移窗口 | 未测量 | — |
| **限流信号是 429 还是别的** | **实测为 HTTP 406 空响应体**，见 §3 | 高 |

---

## 1. 端点与重定向

```powershell
curl.exe -sS -L --max-time 30 -A '<UA>' `
  'https://export.arxiv.org/api/query?search_query=cat%3Acs.CL+AND+submittedDate%3A%5B202401010000+TO+202401072359%5D&start=0&max_results=2&sortBy=submittedDate&sortOrder=descending'
```

结果：`HTTP/1.1 200 OK`，`content-type: application/atom+xml; charset=utf-8`，
`Content-Length: 5306`，正文以 `<?xml version='1.0' encoding='UTF-8'?>` 与
`<feed xmlns:opensearch=... xmlns:arxiv=... xmlns="http://www.w3.org/2005/Atom">` 开头。

响应头（证明链路完整）：

```text
via: 1.1 google, 1.1 varnish, 1.1 varnish, 1.1 varnish
server: Google Frontend
x-cache: MISS, MISS, MISS
x-cloud-trace-context: f8d94fd5976527b8beea8d94228cc852
age: 0
strict-transport-security: max-age=300
```

`HEAD http://export.arxiv.org/api/query` → `301`，`location: https://export.arxiv.org/api/query`
（同域 HTTPS）。**方案 §5.7「HTTP redirect」行关闭**：应用固定用 HTTPS，拒绝跨站重定向。

## 2. `id_list` 查询

```powershell
curl.exe -sS -L --max-time 30 -A '<UA>' `
  'https://export.arxiv.org/api/query?id_list=2101.00001&start=0&max_results=1'
```

结果：`200`，`Content-Length: 2243`，1 个 `<entry>`，
`age: 1246`、`x-cache: MISS, MISS, HIT`（边缘缓存命中）。

## 3. 重大发现：限流以 HTTP 406 空响应体表达，而不是 429

### 3.1 现象

用 `scripts/probe_arxiv_api.py --mode all`（约 25 次请求，起点间隔 3.1 秒）运行时，
大量 `search_query` 请求返回 **HTTP 406，响应体 0 字节，无 `content-type`**：

```text
字段探测（7 个用例）
  !! single_category_with_date status=406 total=None entries=None
  !! multi_category           status=406 total=None entries=None
  !! title_phrase             status=406 total=None entries=None
  !! author_phrase            status=406 total=None entries=None
  !! boolean_andnot           status=406 total=None entries=None
  !! pagination_offset        status=406 total=None entries=None
  !! count_only_forbidden     status=406 total=None entries=None
```

且同一轮中，形状几乎相同的 `page-size` 请求却成功：

```text
OK 请求 100   返回 entries=100 bytes=238298 status=200
!! 请求 200   返回 entries=None bytes=0 status=406
OK 请求 500   返回 entries=500 bytes=1250225 status=200
```

### 3.2 排除的假设

| 假设 | 实验 | 结果 |
| --- | --- | --- |
| 自定义 `Accept` 头导致 406 | 分别用「自定义 `Accept` + 自定义 UA」「httpx 默认头」「仅自定义 UA」「仅自定义 `Accept`」「`Accept-Encoding: identity`」各发 2 次 | **全部 406**，与头无关 |
| `max_results` 取值敏感 | 6 秒间隔分别测 `max_results` = 1 / 50 / 200（带 `sortBy`）/ 裸 `search_query` | **全部 406** |
| 本机代理拦截 | 检查 `HTTP_PROXY`/`HTTPS_PROXY`（无）、WinINET `ProxyEnable=0`、`netsh winhttp show proxy`（直连）、`httpx._utils.get_environment_proxies()`（`{}`） | **无代理参与** |
| 是 httpx 客户端被区别对待 | curl 与 httpx 交替 A/B，各 2 轮，间隔 5 秒，URL 完全相同 | **两者都是 200** |

### 3.3 复核：冷却后仍会立刻复发（请求序号 1 成功、2–7 全败）

冷却到 406 完全消失后，用 `--mode fields --interval 8` 重跑（7 个用例，
起点间隔实测 **8.0 秒**，是官方 3 秒下限的 2.7 倍）：

```text
字段探测（7 个用例）
  OK single_category_with_date status=200 total=173 entries=2
  !! multi_category           status=406 total=None entries=None
  !! title_phrase             status=406 total=None entries=None
  !! author_phrase            status=406 total=None entries=None
  !! boolean_andnot           status=406 total=None entries=None
  !! pagination_offset        status=406 total=None entries=None
  !! count_only_forbidden     status=406 total=None entries=None

共 7 次请求，用时 194.91 秒；最小起点间隔 8.0 秒
[警告] 本轮有 6 次请求被上游以 HTTP 406 空响应体拒绝（请求序号 [2, 3, 4, 5, 6, 7]）
```

关键信息：

- **第 1 个用例成功**，且它正是 curl/httpx A/B 测试里反复命中的那个 URL
  （方案 §5.3 示例 1），返回 `total=173`；
- 之后 6 个**全新**查询全部 406，即使每个 406 之后又强制等待 30 秒；
- 整轮耗时 194.91 秒（8 秒限速 + 6×30 秒退避），仍然全败。

这与「按固定间隔限流」的模型不符：间隔已经放大到 8 秒，退避 30 秒也无效。
更像**「可被边缘缓存满足的请求放行、需要回源的请求被拒」**——所有复现过的
200 都带 `age`/`x-cache: HIT` 或命中常见查询；所有 406 都发生在**首次出现的查询**上。

但该解释**存在反例**：curl 曾在 `x-cache: MISS, MISS, MISS`、`age: 0`、
`server: Google Frontend`（即确实回源）的情况下拿到 200。

因此本文件**不给出机制结论**，只固定可复现的事实与必须采取的工程措施。
继续试探会加重限流，已主动停止。

### 3.4 判定（工程上唯一安全的结论）

**`search_query` 会间歇性返回 HTTP 406 空响应体；它与请求头、代理、客户端库无关；
冷却后可恢复，但在持续查询下极易复发。实现必须把它当成「上游限流/拒绝」处理。**

证据：

1. 406 响应与 200 响应的链路深度不同：
   - 200：`via: 1.1 google, 1.1 varnish×3` + `server: Google Frontend` + `x-cloud-trace-context`
   - 406：`via: 1.1 varnish, 1.1 varnish`，**无 `server`**，`cache-control: private, no-store`
   
   即 406 由更靠前的边缘层直接返回，没有到达源站；
2. 与自定义 `Accept`、UA、`Accept-Encoding` 全部无关（5 组对照实验全部 406）；
3. 与 `max_results` 取值无关（1 / 50 / 200 / 裸查询全部 406）；
4. 与客户端库无关（curl 与 httpx 交替 A/B 各 2 轮全部 200，完全相同 URL）；
5. 本机无代理参与（环境变量为空、WinINET 关闭、WinHTTP 直连、httpx 代理表为 `{}`）；
6. 冷却后完全相同的 httpx 请求恢复 200。

### 3.5 对实现的影响（**必须在 T3.1/T3.2 落实**）

1. **`arxiv/client.py` 必须把 406 当作可重试的上游限流**，而不是
   `QueryContractError`。方案 §5.4 只列了 429/5xx 的重试策略，需要补 406：
   - 进入与 429 相同的长退避路径（≥30 秒），并计入熔断；
   - **绝不**在 406 时归因于用户的关键词语法，也不要向非技术用户显示「406」。
2. **限速间隔不能贴着 3.0 秒**。方案 §5.4 的 3.1 秒余量在实测中不足以避免 406
   （实测最小起点间隔 3.093 秒，低于目标值——Windows `time.sleep` 会提前返回）。
   建议 T3.1 把默认间隔提高到 **≥4 秒**，并用 deadline 循环实现而非单次 `sleep`。
3. **必须提供「重试 / 使用过期缓存」的用户出口**：406 会持续一段时间，
   此时唯一可用的是 stale cache（方案 §4.8 已为该场景设计了文案，
   复用 `ARXIV_RATE_LIMITED` 的中文提示即可）。
4. **验收口径要放宽**：方案 §7.7 A3「无未处理 429/503」需要扩展为
   「无未处理 429/503/**406**」。
5. 探测工具已相应加固：默认间隔 4.5 秒（`--interval` 可调）、命中 406 额外退避
   30 秒并记录 `throttle_hits`、退出码 2 表示「本轮被限流，结论不可信」、
   `classify()` 记录 `via`/`x-cache`/`server`/`age` 等 CDN 头。

### 3.6 仍未解释的部分

- 406 的**确切触发条件与配额未知**。已知上界很紧：约 7 次连续新查询即可触发；
  也已观察到约 25 次连续请求触发。
- 「缓存命中放行、回源被拒」这一解释有反例（见 §3.3），**未验证**。
- 406 的恢复时间未精确测量，只知道「数分钟到十几分钟」量级可恢复。

> ⚠️ **未决风险**：如果 406 在真实使用中（例如一次 2000 条、10 页的抓取）稳定出现，
> 产品的主查询路径将不可用。这需要在 T3 阶段用真实负载重新验证，
> 并准备缓解方案（更大的间隔、请求合并、更依赖缓存、必要时改用 OAI-PMH 预热索引）。
> 已登记到风险清单。

## 4. 未关闭的 `【待实测验证】` 项

以下项**本轮未测**，保持未决；发布前必须补测，并记录到本文件：

| 命题 | 原因 |
| --- | --- |
| 有效偏移窗口（10000 边界） | `--mode window` 需要最多 20 次请求；在限流行为未摸清前不跑 |
| 单次切片上限（200 / 500 / 2000） | 同上 |
| `max_results=0` 的行为 | 已知会触发一次 500；本轮未复测 |
| POST form 与 GET 是否等价 | 未测（方案要求「不用 POST」，不影响实现） |
| 大小写敏感性 | 未取得可信结果（被 406 打断） |
| 中文关键词 | 未取得可信结果（被 406 打断） |
| 特殊字面量（`C++`、连字符等） | 未取得可信结果（被 406 打断） |
| 官网数量 ±5% 对照 | 需人工按方案 §7.7 A2 执行 |

**复测要求**：至少冷却 10 分钟，用 `--interval 8`，且一次只跑一个模式；
出现退出码 2 时结果作废，重新冷却后再跑。

## 5. 与官方文档的差异登记

| 事项 | 官方文档 | 实测 | 处理 |
| --- | --- | --- | --- |
| 限流响应码 | 文档只说明「每 3 秒最多 1 请求」 | 超限时返回 **406 空响应体**（非 429） | 代码同时处理 406 与 429；本文件为准 |
| 限速安全间隔 | 3 秒 | 3.1 秒间隔仍会触发 406 | 实现用 ≥4 秒 |
| 单次切片 2000 / 总窗口 30000 | 手册值 | 未复测；历史实测为 `start+max_results>10000` 失败 | 应用用 9500 安全值，发布前复测 |
| `max_results=0` | 未说明 | 历史实测 HTTP 500 | 禁止 count-only；首包同时读取 `total` |

## 6. 原始产物

| 文件 | 内容 | 备注 |
| --- | --- | --- |
| `docs/artifacts/probe-all-throttled.json` | `--mode all` 的完整 JSON（25 次请求，间隔 3.1 秒） | **含被 406 拒绝的轮次**，仅作限流证据，不可作契约结论。该文件生成于「406 专用分类」加入之前，因此其中的 406 被记为 `non_xml_content_type` |
| `docs/artifacts/probe-fields-interval8-throttled.json` | `--mode fields --interval 8` 的完整 JSON（7 次请求） | 请求序号 1 成功、2–7 被 406 拒绝；含 `via`/`x-cache`/`server` 证据 |

两个文件都保留了 `observed_min_interval_s`，可直接核对限速是否被遵守：

- `docs/artifacts/probe-all-throttled.json`：`3.093` 秒（**低于** 3.1 秒目标，证明 `time.sleep` 会提前返回）
- `docs/artifacts/probe-fields-interval8-throttled.json`：`8.0` 秒（符合），但仍有 6/7 请求被 406 拒绝

### 可用作契约结论的正向数据（来自上述轮次中成功的请求）

| 查询 | total | entries | 说明 |
| --- | --- | --- | --- |
| `id_list=2101.00001` | 1 | 1 | `id_list` 稳定可用 |
| `cat:cs.CL` + `max_results=100` + `submittedDate desc` | 119894 | 100 | 响应 238298 字节 |
| `cat:cs.CL` + `max_results=500` + `submittedDate desc` | 119894 | 500 | 响应 1250225 字节（1.19 MiB，远低于 20 MiB 上限） |
| 方案 §5.3 示例 1（cs.CL + 2024-01-01..07） | 173 | 2 | `feed_updated` 与 `opensearch` 字段齐全 |

命名空间实测固定为三个：

```text
http://www.w3.org/2005/Atom
http://a9.com/-/spec/opensearch/1.1/
http://arxiv.org/schemas/atom
```
