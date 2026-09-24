"""真实访问 arXiv 的契约验证（方案 §7.1「Live contract」层）。

**默认被跳过。** 只有显式设置 ``ARXIV_LIVE_TESTS=1`` 才会执行，且必须串行运行。

这些测试回答的是离线测试回答不了的问题：

* 上游此刻是否可用、返回的 Atom 是否仍符合预期；
* **HTTP 406 限流是否会在真实负载下阻断主查询路径**——
  这是 2026-09-24 发现的唯一红色未关闭风险（见 ``docs/api-verification.md`` §3）。

运行方式（务必一次只跑一个，并在两次运行之间充分冷却 ≥10 分钟）::

    $env:ARXIV_LIVE_TESTS = "1"
    python -m pytest tests/live -q -s

不要为了「跑绿」而反复重跑：触发限流后继续请求只会延长冷却时间。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from arxiv_filter.arxiv.atom_parser import parse_atom
from arxiv_filter.arxiv.client import ArxivClient, ClientConfig
from arxiv_filter.arxiv.pagination import Paginator
from arxiv_filter.arxiv.query_builder import build_query
from arxiv_filter.arxiv.rate_limiter import RateLimiter
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.query import BuiltQuery, SearchRequest, SortMode
from arxiv_filter.taxonomy import load_taxonomy

pytestmark = pytest.mark.live_arxiv

ARTIFACTS = Path(__file__).resolve().parents[2] / "docs" / "artifacts"

#: 真实负载验证用的间隔。取方案默认值（4.0 秒），不做额外放宽——
#: 如果 4.0 秒过不了，那就是产品默认值需要改，而不是测试需要放宽。
LIVE_INTERVAL_S = 4.0


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def record(name: str, payload: dict) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    target = ARTIFACTS / name
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n[live] 结果已写入 {target}")


@pytest.mark.asyncio
async def test_live_id_list_smoke() -> None:
    """最小连通性检查：一个已知 ID，一条请求。

    对应方案 §5.6：这是最不容易触发限流的请求形状。
    """
    limiter = RateLimiter(LIVE_INTERVAL_S)
    client = ArxivClient(limiter=limiter, config=ClientConfig())
    try:
        response = await client._client.get(  # noqa: SLF001
            client.config.api_url,
            params={"id_list": "2101.00001", "start": 0, "max_results": 1},
        )
        print(f"\n[live] id_list smoke → HTTP {response.status_code}")
        if response.status_code != 200:
            pytest.skip(f"上游当前不可用（HTTP {response.status_code}），跳过")

        parsed = parse_atom(response.content, datetime.now(timezone.utc))
        assert parsed.papers, "连通性检查应当至少返回一条记录"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_search_query_pagination_under_real_load() -> None:
    """**风险验证**：真实负载下 ``search_query`` 分页是否会被 406 阻断。

    场景对标方案 §7.7 A2：cs.CL + 最近 7 个 UTC 自然日，但把 cap 压到 600
    （3 页）以便把请求量控制在最小可判定规模——先回答「能不能连续翻 3 页」，
    再决定要不要上到 2000 条 / 10 页。

    本测试**不断言必须成功**：它把结果记录下来供人工判定。
    断言的是「失败必须是已处理的领域错误，而不是崩溃或误判成语法错误」。
    """
    start_day = today_utc() - timedelta(days=7)
    end_day = today_utc() - timedelta(days=1)

    request = SearchRequest(
        group_id="cs",
        category_id="cs.CL",
        start_date=start_day,
        end_date=end_day,
        keywords='"large language model"',
        sort=SortMode.SUBMITTED_DESC,
        result_cap=600,
    )
    table = load_taxonomy()
    query = build_query(request, taxonomy=table)

    limiter = RateLimiter(LIVE_INTERVAL_S)
    client = ArxivClient(limiter=limiter, config=ClientConfig())
    pager = Paginator(client, page_size=200)

    started = time.monotonic()
    outcome: dict[str, object] = {
        "scenario": "cs.CL + last 7 UTC days + phrase, cap=600",
        "interval_s": LIVE_INTERVAL_S,
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "expression": query.expression,
        "started_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    error: ArxivFilterError | None = None
    try:
        result = await pager.fetch_all(query, result_cap=600)
        outcome |= {
            "status": "completed",
            "papers": len(result.papers),
            "pages": result.pages,
            "total_reported": result.total_reported,
            "shards": result.shards,
            "warnings": result.warnings,
        }
    except ArxivFilterError as exc:
        error = exc
        outcome |= {
            "status": "failed",
            "error_code": str(exc.code),
            "retryable": exc.retryable,
            "details": {k: v for k, v in exc.details.items() if k != "body_prefix"},
        }
    finally:
        await client.aclose()

    outcome |= {
        "elapsed_s": round(time.monotonic() - started, 1),
        "requests_sent": limiter.grants,
        "min_observed_interval_s": limiter.min_observed_interval(),
    }
    record("live-406-validation.json", outcome)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))

    # —— 无论成败都必须成立的断言 ——

    # 1) 限速真的生效了
    observed = limiter.min_observed_interval()
    if observed is not None:
        assert observed >= LIVE_INTERVAL_S - 0.01, f"限速未生效：{observed}"

    # 2) 失败必须是**已处理**的领域错误，且不得把限流说成用户的关键词问题
    if error is not None:
        assert error.code in {
            ErrorCode.ARXIV_NOT_ACCEPTABLE,   # 406 限流
            ErrorCode.ARXIV_RATE_LIMITED,     # 429
            ErrorCode.ARXIV_UNAVAILABLE,      # 5xx / 熔断
            ErrorCode.ARXIV_TIMEOUT,
            ErrorCode.ARXIV_DNS_FAILURE,
            ErrorCode.ARXIV_CONNECT_FAILURE,
        }, f"未预期的错误码：{error.code}"
        assert error.code is not ErrorCode.INVALID_KEYWORD_SYNTAX
        assert "406" not in error.message_zh and "关键词" not in error.message_zh
