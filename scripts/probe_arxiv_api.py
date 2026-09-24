#!/usr/bin/env python
"""arXiv legacy API 契约与能力探测（方案 T0.2 / §5.7）。

设计约束（全部来自方案）：

* **所有**请求（含重试）经过同一个全局限速器，任两次实际发送的 monotonic
  时间差 ≥ ``--interval`` 秒（默认 4.5 秒；官方条款是每 3 秒最多 1 请求、
一次一个连接，但实测贴着 3 秒会被上游以 406 限流）；
* 失败只保存 ≤ 1 KiB 的**脱敏**响应摘要，绝不落盘完整正文或代理凭据；
* 输出机器可读 JSON（status / content-type / namespace / total / Atom error /
  entry 数），供 ``docs/api-verification.md`` 直接引用；
* 只做探测，不做业务逻辑；不修改任何产品代码或缓存。

用法::

    python scripts/probe_arxiv_api.py --mode smoke
    python scripts/probe_arxiv_api.py --mode redirect
    python scripts/probe_arxiv_api.py --mode fields
    python scripts/probe_arxiv_api.py --mode special
    python scripts/probe_arxiv_api.py --mode page-size --sizes 100,200,500
    python scripts/probe_arxiv_api.py --mode window --query 'cat:cs.CL'
    python scripts/probe_arxiv_api.py --mode all --json-out probe.json

每条命令都会真实联网。**不要**在三秒内连续执行多条；本脚本自己会串行化，
但多个脚本实例之间不会互相协调，所以请一次只跑一个。

注意：``window`` 模式按方案 §5.7 最多发 20 次请求，人工在发布前运行。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import httpx
except ImportError:  # pragma: no cover - 环境未装依赖
    print(
        "缺少 httpx。请先安装项目依赖：\n"
        "  D:\\miniforge3\\Scripts\\conda.exe run -n arxiv-filter python -m pip install -e .",
        file=sys.stderr,
    )
    raise SystemExit(2)

try:
    from defusedxml import ElementTree as SafeET
except ImportError:  # pragma: no cover - 环境未装依赖
    print("缺少 defusedxml。请先安装项目依赖。", file=sys.stderr)
    raise SystemExit(2)


def _force_utf8_stdio() -> None:
    """把标准输出/错误切到 UTF-8（Windows 控制台默认编码会让中文变乱码）。

    刻意只在 :func:`main` 里调用：导入模块不应产生全局副作用。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 流不支持重配置
                pass


API_URL = "https://export.arxiv.org/api/query"
HTTP_URL = "http://export.arxiv.org/api/query"
USER_AGENT = "arxiv-filter-connectivity-check/0.1 (personal research client)"
APP_VERSION = "0.1.0"

#: 官方限速的文档下限：每 3 秒最多 1 请求。
DOCUMENTED_INTERVAL_S = 3.0

#: 探测默认间隔。**刻意大于方案的 3.1 秒**：2026-09-24 实测，按 3.1 秒间隔连发
#: 约 25 次后，``search_query`` 请求开始被边缘节点以 HTTP 406 空响应体拒绝，
#: 冷却数分钟后同样的请求恢复 200。客户端 3.1 秒的间隔在链路上会被抖动吃掉，
#: 落到源站时可能低于 3.0 秒。探测工具用更保守的值，避免自己把源站打进限流。
MIN_INTERVAL_S = 4.5

#: 命中 406 后额外等待的秒数（限流信号，见 :data:`MIN_INTERVAL_S`）。
THROTTLE_BACKOFF_S = 30.0

#: 诊断摘要上限（方案 §5.4：最多 1 KiB）。
MAX_DIAGNOSTIC_BYTES = 1024

#: 单次响应体读取上限，防止异常大响应打爆内存。
MAX_BODY_BYTES = 20 * 1024 * 1024

#: 超时（方案 §4.8：connect 10 / read 30 / write 10 / pool 10）。
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "o": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

#: 特殊字面量表（方案 §5.7「特殊字面量」行）。值是要放进 search_query 的原文。
SPECIAL_LITERALS: tuple[str, ...] = (
    'all:"C++"',
    "all:state-of-the-art",
    "ti:attention",
    'all:"multi-agent"',
    "all:Transformer",
    'abs:"neural network"',
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s@]+)@")
#: 连值一起遮蔽：``Authorization: Bearer abc`` / ``api_key=zzz`` / ``password=hunter2``。
#: 只遮蔽关键字本身会留下凭据明文，所以整段替换为 ``***``。
_SECRET_RE = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|access[_-]?token|token|password|passwd|secret)"
    r"\b\s*[:=]\s*(?:bearer\s+|basic\s+)?\S+"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text: str) -> str:
    """遮蔽 URL 凭据与常见密钥字段，供诊断输出使用。"""
    text = _CREDENTIAL_RE.sub(r"\1***@", text)
    text = _SECRET_RE.sub("***", text)
    return text


def sanitize_body(body: bytes) -> str:
    """把响应正文压成 ≤ 1 KiB 的脱敏单行摘要。"""
    text = body[:MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace")
    text = _CONTROL_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    text = redact(" ".join(text.split()))
    return text


class MinIntervalLimiter:
    """全进程单连接、最小发送间隔限速器。

    阶段 0 的实现刻意自包含，好让本脚本在没有完整包时也能单独运行；
    T3.1 会把同一语义提取到 ``arxiv_filter.arxiv.rate_limiter``。
    """

    def __init__(self, min_interval_s: float = MIN_INTERVAL_S) -> None:
        self.min_interval_s = min_interval_s
        self._last_sent_at: float | None = None
        self.send_count = 0

    def acquire(self) -> float:
        """阻塞到可以发送，返回本次实际等待的秒数。

        用 **deadline 循环**而不是单次 ``sleep``：Windows 的 ``time.sleep`` 可能
        提前返回，单次调用会让实际间隔掉到 3.1 秒以下——2026-09-24 实测到
        3.093 秒，而官方下限是 3.0 秒，余量会被网络抖动吃光。
        """
        waited = 0.0
        if self._last_sent_at is not None:
            deadline = self._last_sent_at + self.min_interval_s
            for _ in range(5):  # 防御性上界，避免时钟异常导致死循环
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(remaining)
                waited += remaining
        self._last_sent_at = time.monotonic()
        self.send_count += 1
        return waited


@dataclass
class ProbeClient:
    """一个遵守限速的探测客户端。"""

    limiter: MinIntervalLimiter = field(default_factory=MinIntervalLimiter)
    client: httpx.Client = field(default_factory=httpx.Client)
    send_starts: list[float] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)
    #: 命中 HTTP 406 的请求序号（1 起）。用于判断本轮结果是否可信。
    throttle_hits: list[int] = field(default_factory=list)

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """发一次请求；无论成功失败都先过限速器。

        命中 406 时额外等待 :data:`THROTTLE_BACKOFF_S`，因为实测 406 是
        上游的限流信号而不是查询语法错误——继续按原速发只会让情况更糟。
        """
        self.limiter.acquire()
        self.send_starts.append(time.monotonic())
        response = self.client.request(
            method,
            url,
            params=params,
            data=data,
            headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/xml"},
            follow_redirects=follow_redirects,
        )
        if response.status_code == 406:
            self.throttle_hits.append(len(self.send_starts))
            time.sleep(THROTTLE_BACKOFF_S)
        return response

    def get_api(self, params: dict[str, Any]) -> httpx.Response:
        return self.request("GET", API_URL, params=params)

    @property
    def throttled(self) -> bool:
        """本轮是否出现过 406；出现即表示结果可能不完整。"""
        return bool(self.throttle_hits)

    def min_observed_interval(self) -> float | None:
        """已发送请求之间观察到的最小起点间隔。"""
        if len(self.send_starts) < 2:
            return None
        return min(
            b - a for a, b in zip(self.send_starts, self.send_starts[1:], strict=False)
        )


#: 需要在每次探测结果里保留的响应头。CDN 层的行为（varnish / x-cache / via）
#: 是区分「契约变化」与「源站限流」的关键证据，必须留存。
CAPTURED_HEADERS: tuple[str, ...] = (
    "content-type",
    "content-length",
    "server",
    "via",
    "x-cache",
    "x-cache-hits",
    "x-served-by",
    "age",
    "cache-control",
    "retry-after",
    "x-request-id",
    "vary",
    "date",
)


def classify(response: httpx.Response) -> dict[str, Any]:
    """把一次响应归类为结构化探测结果（方案 §5.7 的 ``classify_probe``）。

    探测成功**不能只看 200**：500 也可能是 Atom error entry，503 可能是 HTML，
    429 可能是纯文本。本函数还额外记录 CDN 响应头，因为 2026-09 实测发现
    ``search_query`` 请求会被边缘节点以 **HTTP 406 空响应体**拒绝，
    这类失败在只看状态码时极易被误判成「查询语法错误」。
    """
    content_type = response.headers.get("content-type", "")
    result: dict[str, Any] = {
        "status": response.status_code,
        "content_type": content_type,
        "url": redact(str(response.url)),
        "retry_after": response.headers.get("retry-after"),
        "content_length": len(response.content),
        "headers": {
            key: response.headers[key] for key in CAPTURED_HEADERS if key in response.headers
        },
    }

    if response.status_code == 406:
        # 实测：406 的响应体为空、无 content-type，且与自定义头无关。
        # 归为「上游拒绝服务」而不是「查询语法错误」，以便调用方按可重试处理。
        return result | {
            "ok": False,
            "reason": "http_406_not_acceptable",
            "body_prefix": sanitize_body(response.content),
            "note": (
                "406 且响应体为空：疑似边缘节点/CDN 层拒绝，而非查询语法问题。"
                "应按可重试的上游错误处理，并记录 headers 以便与限流区分。"
            ),
        }

    if "xml" not in content_type.lower() and "atom" not in content_type.lower():
        return result | {
            "ok": False,
            "reason": "non_xml_content_type",
            "body_prefix": sanitize_body(response.content),
        }

    try:
        root = SafeET.fromstring(response.content)
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都要归为 bad_xml
        return result | {
            "ok": False,
            "reason": "xml_parse_error",
            "error": type(exc).__name__,
            "body_prefix": sanitize_body(response.content),
        }

    entries = root.findall("a:entry", NS)
    error_entries = [
        {"title": entry.findtext("a:title", default="", namespaces=NS),
         "summary": entry.findtext("a:summary", default="", namespaces=NS)[:300]}
        for entry in entries
        if entry.findtext("a:title", default="", namespaces=NS).strip() == "Error"
    ]
    namespace_of_root = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    return result | {
        "ok": response.status_code == 200 and not error_entries,
        "root_tag": namespace_of_root,
        "root_namespace": root.tag[1:].split("}")[0] if root.tag.startswith("{") else "",
        "atom_error": bool(error_entries),
        "error_entries": error_entries,
        "entry_count": len(entries),
        "total": root.findtext("o:totalResults", namespaces=NS),
        "start_index": root.findtext("o:startIndex", namespaces=NS),
        "items_per_page": root.findtext("o:itemsPerPage", namespaces=NS),
        "feed_updated": root.findtext("a:updated", namespaces=NS),
        "namespaces_seen": sorted(
            {
                el.tag[1:].split("}")[0]
                for el in root.iter()
                if isinstance(el.tag, str) and el.tag.startswith("{")
            }
        ),
    }


def probe_redirect(probe: ProbeClient) -> dict[str, Any]:
    """HTTP → HTTPS 重定向是否仍在同域（方案 §5.7「HTTP redirect」行）。"""
    response = probe.request("HEAD", HTTP_URL)
    location = response.headers.get("location", "")
    return {
        "probe": "redirect",
        "http_status": response.status_code,
        "location": redact(location),
        "same_host": location.startswith("https://export.arxiv.org/"),
        "ok": response.status_code in (301, 302, 307, 308)
        and location.startswith("https://export.arxiv.org/"),
    }


def probe_smoke(probe: ProbeClient) -> dict[str, Any]:
    """最小连通性检查：一个已知 ID，一条请求。"""
    response = probe.get_api({"id_list": "2101.00001", "start": 0, "max_results": 1})
    outcome = classify(response)
    return {"probe": "smoke", **outcome}


def probe_fields(probe: ProbeClient, query: str = "cat:cs.CL") -> dict[str, Any]:
    """字段与前缀覆盖面：分类、日期、标题短语、作者、布尔、排序、分页。"""
    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "single_category_with_date",
            {
                "search_query": "cat:cs.CL AND submittedDate:[202401010000 TO 202401072359]",
                "start": 0,
                "max_results": 2,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
        ),
        (
            "multi_category",
            {
                "search_query": "(cat:cs.CL OR cat:cs.AI) AND submittedDate:[202401010000 TO 202401032359]",
                "start": 0,
                "max_results": 2,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
        ),
        (
            "title_phrase",
            {
                "search_query": 'ti:"large language model"',
                "start": 0,
                "max_results": 2,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        ),
        (
            "author_phrase",
            {
                "search_query": 'au:"Geoffrey Hinton"',
                "start": 0,
                "max_results": 2,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
        ),
        (
            "boolean_andnot",
            {
                "search_query": "(ti:transformer OR abs:transformer) ANDNOT cat:cs.CV",
                "start": 0,
                "max_results": 2,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        ),
        (
            "pagination_offset",
            {
                "search_query": "cat:cs.CL",
                "start": 10,
                "max_results": 5,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
        ),
        (
            "count_only_forbidden",
            {"search_query": "cat:cs.CL", "start": 0, "max_results": 0},
        ),
    ]

    results = []
    for name, params in cases:
        response = probe.get_api(params)
        outcome = classify(response)
        results.append({"case": name, "params": params, **outcome})
    return {"probe": "fields", "default_query": query, "cases": results}


def probe_case_sensitivity(probe: ProbeClient) -> dict[str, Any]:
    """大小写是否影响命中数（方案 §5.7「大小写」行）。"""
    upper = probe.get_api({"search_query": 'ti:"LARGE LANGUAGE MODEL"', "max_results": 1})
    lower = probe.get_api({"search_query": 'ti:"large language model"', "max_results": 1})
    upper_outcome, lower_outcome = classify(upper), classify(lower)
    same_total = upper_outcome.get("total") == lower_outcome.get("total")
    return {
        "probe": "case_sensitivity",
        "upper": upper_outcome,
        "lower": lower_outcome,
        "same_total": same_total,
        "ok": bool(same_total and upper_outcome.get("ok") and lower_outcome.get("ok")),
    }


def probe_chinese(probe: ProbeClient) -> dict[str, Any]:
    """中文查询是否被接受（方案 §5.7「中文」行）。"""
    response = probe.get_api({"search_query": "all:机器学习", "max_results": 1})
    outcome = classify(response)
    return {"probe": "chinese", **outcome}


def probe_special(probe: ProbeClient) -> dict[str, Any]:
    """特殊字面量表（方案 §5.7「特殊字面量」行）。"""
    results = []
    for literal in SPECIAL_LITERALS:
        response = probe.get_api({"search_query": literal, "max_results": 1})
        results.append({"literal": literal, **classify(response)})
    return {"probe": "special", "literals": results}


def probe_post_form(probe: ProbeClient, query: str = "cat:cs.CL") -> dict[str, Any]:
    """POST form 是否与 GET 等价（方案 §5.7「POST form」行）。"""
    params = {"search_query": query, "start": 0, "max_results": 1, "sortBy": "submittedDate"}
    get_response = probe.get_api(params)
    get_outcome = classify(get_response)
    post_response = probe.request("POST", API_URL, data=params)
    post_outcome = classify(post_response)

    get_first = _first_entry_id(get_response)
    post_first = _first_entry_id(post_response)
    same_first = get_first is not None and get_first == post_first
    return {
        "probe": "post_form",
        "get": get_outcome | {"first_entry_id": get_first},
        "post": post_outcome | {"first_entry_id": post_first},
        "same_first_entry": same_first,
        "ok": bool(get_outcome.get("ok") and post_outcome.get("ok") and same_first),
        "note": "方案结论：不使用 POST；长分类表达式拆成多个顺序 GET。",
    }


def _first_entry_id(response: httpx.Response) -> str | None:
    try:
        root = SafeET.fromstring(response.content)
    except Exception:  # noqa: BLE001
        return None
    return root.findtext("a:entry/a:id", namespaces=NS)


def probe_page_size(probe: ProbeClient, sizes: Sequence[int], query: str) -> dict[str, Any]:
    """页大小稳定性与响应体积（方案 §5.7「page size」行）。"""
    results = []
    for size in sizes:
        response = probe.get_api(
            {
                "search_query": query,
                "start": 0,
                "max_results": size,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        outcome = classify(response)
        results.append(
            {
                "requested": size,
                "bytes": len(response.content),
                "under_20mib": len(response.content) < MAX_BODY_BYTES,
                **outcome,
            }
        )
    return {"probe": "page_size", "query": query, "sizes": list(sizes), "results": results}


def probe_total_stability(probe: ProbeClient, query: str) -> dict[str, Any]:
    """同一查询连续两次的 total 是否稳定（方案 §5.7「total 稳定」行）。"""
    first = probe.get_api({"search_query": query, "start": 0, "max_results": 1})
    second = probe.get_api({"search_query": query, "start": 0, "max_results": 1})
    first_outcome, second_outcome = classify(first), classify(second)
    first_total = first_outcome.get("total")
    second_total = second_outcome.get("total")
    # 两次都失败时 total 都是 None，绝不能因此报「稳定」——那是假阳性。
    both_ok = bool(first_outcome.get("ok") and second_outcome.get("ok"))
    compared = both_ok and first_total is not None and second_total is not None
    return {
        "probe": "total_stability",
        "query": query,
        "first_total": first_total,
        "second_total": second_total,
        "compared": compared,
        "stable": compared and first_total == second_total,
        "ok": compared,
        "first_feed_updated": first_outcome.get("feed_updated"),
        "second_feed_updated": second_outcome.get("feed_updated"),
        "note": (
            ""
            if compared
            else "至少一次请求未成功，本轮无法判定 total 是否稳定。"
        ),
    }


def probe_window(
    probe: ProbeClient,
    query: str,
    *,
    max_requests: int = 20,
    ceiling: int = 40000,
) -> dict[str, Any]:
    """测量当前有效的分页偏移窗口（方案 §5.7「effective window」行）。

    先用 1000/2000/4000/… 指数探测 ``start=n-1, max_results=1``，再在最后
    成功与首次失败之间二分。全程共用同一个限速器，请求数受 ``max_requests`` 约束。
    """
    attempts: list[dict[str, Any]] = []
    budget = max_requests

    def one(start: int) -> dict[str, Any]:
        nonlocal budget
        if budget <= 0:
            raise RuntimeError("window 探测请求预算已用尽")
        budget -= 1
        response = probe.get_api(
            {
                "search_query": query,
                "start": start,
                "max_results": 1,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        outcome = classify(response)
        record = {"start": start, "max_results": 1, **outcome}
        attempts.append(record)
        return record

    # 1) 指数探测：找最后一个成功点与第一个失败点
    last_ok: int | None = None
    first_fail: int | None = None
    step = 1000
    while step <= ceiling and budget > 1:
        record = one(step - 1)
        if record.get("ok"):
            last_ok = step - 1
            step *= 2
        else:
            first_fail = step - 1
            break

    # 2) 二分收敛到 1 的精度
    if last_ok is not None and first_fail is not None:
        low, high = last_ok, first_fail
        while high - low > 1 and budget > 0:
            mid = (low + high) // 2
            record = one(mid)
            if record.get("ok"):
                low = mid
            else:
                high = mid
        last_ok, first_fail = low, high

    return {
        "probe": "offset_window",
        "query": query,
        "requests_used": len(attempts),
        "last_successful_start": last_ok,
        "first_failing_start": first_fail,
        "max_results_per_request": 1,
        "ok": last_ok is not None,
        "attempts": attempts,
        "conclusion": (
            f"start + max_results <= {last_ok + 2} 可用"
            if last_ok is not None
            else "未找到可用偏移，请检查网络或查询表达式"
        ),
        "note": "应用侧安全值取 9500，本探测只记录当前实测边界。",
    }


MODES = (
    "smoke",
    "redirect",
    "fields",
    "case-sensitivity",
    "chinese",
    "special",
    "post",
    "page-size",
    "total-stability",
    "window",
    "all",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_arxiv_api.py",
        description=(
            f"探测 arXiv legacy API 的契约与运行能力"
            f"（真实联网，自动限速，默认间隔 {MIN_INTERVAL_S} 秒）。"
        ),
    )
    parser.add_argument("--mode", choices=MODES, default="smoke", help="探测模式，默认 smoke。")
    parser.add_argument(
        "--query",
        default="cat:cs.CL",
        help="window / page-size / total-stability 使用的查询，默认 cat:cs.CL。",
    )
    parser.add_argument(
        "--sizes",
        default="100,200,500",
        help="page-size 模式的页大小列表，逗号分隔，默认 100,200,500。",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=20,
        help="window 模式的请求预算上限，默认 20。",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=MIN_INTERVAL_S,
        metavar="SECONDS",
        help=(
            f"请求起点最小间隔，默认 {MIN_INTERVAL_S} 秒。"
            f"官方下限是 {DOCUMENTED_INTERVAL_S} 秒，低于 4 秒容易触发上游 406 限流。"
        ),
    )
    parser.add_argument("--json-out", metavar="PATH", help="把结果 JSON 写入文件。")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只输出 JSON，不打印人类可读摘要。",
    )
    return parser


def run_mode(mode: str, probe: ProbeClient, args: argparse.Namespace) -> dict[str, Any]:
    if mode == "smoke":
        return {"mode": mode, "results": [probe_smoke(probe)]}
    if mode == "redirect":
        return {"mode": mode, "results": [probe_redirect(probe)]}
    if mode == "fields":
        return {"mode": mode, "results": [probe_fields(probe, args.query)]}
    if mode == "case-sensitivity":
        return {"mode": mode, "results": [probe_case_sensitivity(probe)]}
    if mode == "chinese":
        return {"mode": mode, "results": [probe_chinese(probe)]}
    if mode == "special":
        return {"mode": mode, "results": [probe_special(probe)]}
    if mode == "post":
        return {"mode": mode, "results": [probe_post_form(probe, args.query)]}
    if mode == "page-size":
        sizes = [int(part) for part in args.sizes.split(",") if part.strip()]
        return {"mode": mode, "results": [probe_page_size(probe, sizes, args.query)]}
    if mode == "total-stability":
        return {"mode": mode, "results": [probe_total_stability(probe, args.query)]}
    if mode == "window":
        return {
            "mode": mode,
            "results": [probe_window(probe, args.query, max_requests=args.max_requests)],
        }
    if mode == "all":
        results: list[dict[str, Any]] = [
            probe_smoke(probe),
            probe_redirect(probe),
            probe_fields(probe, args.query),
            probe_case_sensitivity(probe),
            probe_chinese(probe),
            probe_special(probe),
            probe_post_form(probe, args.query),
            probe_page_size(probe, [int(p) for p in args.sizes.split(",") if p.strip()], args.query),
            probe_total_stability(probe, args.query),
        ]
        return {"mode": mode, "results": results, "note": "window 模式需单独运行，避免超预算。"}
    raise ValueError(f"未知模式：{mode}")


def summarise(payload: dict[str, Any]) -> str:
    lines = [f"模式：{payload['mode']}"]
    for result in payload["results"]:
        name = result.get("probe", "?")
        if "cases" in result:
            lines.append(f"  字段探测（{len(result['cases'])} 个用例）")
            for case in result["cases"]:
                mark = "OK " if case.get("ok") else "!! "
                lines.append(
                    f"    {mark}{case['case']:<24} status={case.get('status')} "
                    f"total={case.get('total')} entries={case.get('entry_count')}"
                )
        elif "literals" in result:
            lines.append(f"  特殊字面量（{len(result['literals'])} 个）")
            for item in result["literals"]:
                mark = "OK " if item.get("ok") else "!! "
                lines.append(
                    f"    {mark}{item['literal']:<28} status={item.get('status')} "
                    f"total={item.get('total')}"
                )
        elif "attempts" in result:
            lines.append(
                f"  偏移窗口：最后成功 start={result['last_successful_start']}，"
                f"首个失败 start={result['first_failing_start']}，"
                f"用掉 {result['requests_used']} 次请求"
            )
            lines.append(f"  结论：{result['conclusion']}")
        elif name == "redirect":
            mark = "OK " if result.get("ok") else "!! "
            lines.append(
                f"  {mark}redirect: http_status={result.get('http_status')} "
                f"location={result.get('location')!r} same_host={result.get('same_host')}"
            )
        elif name == "total_stability":
            mark = "OK " if result.get("ok") else "!! "
            lines.append(
                f"  {mark}total 稳定性: {result.get('first_total')} vs "
                f"{result.get('second_total')} → stable={result.get('stable')}"
            )
            if result.get("note"):
                lines.append(f"      {result['note']}")
        elif "results" in result:
            lines.append(f"  页大小探测（{len(result['results'])} 项）")
            for item in result["results"]:
                mark = "OK " if item.get("ok") else "!! "
                lines.append(
                    f"    {mark}请求 {item['requested']:<5} 返回 entries="
                    f"{item.get('entry_count')} bytes={item['bytes']} status={item.get('status')}"
                )
        else:
            mark = "OK " if result.get("ok") else "!! "
            lines.append(
                f"  {mark}{name}: status={result.get('status')} "
                f"entries={result.get('entry_count')} total={result.get('total')}"
            )
            if not result.get("ok") and result.get("body_prefix"):
                lines.append(f"      body: {result['body_prefix'][:160]}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    probe = ProbeClient(
        limiter=MinIntervalLimiter(args.interval),
        client=httpx.Client(timeout=TIMEOUT, follow_redirects=False),
    )
    started = time.monotonic()
    try:
        payload = run_mode(args.mode, probe, args)
    except KeyboardInterrupt:  # pragma: no cover - 人工中断
        print("\n已人工中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - 探测脚本必须给出可读失败
        print(f"探测失败：{type(exc).__name__}: {redact(str(exc))}", file=sys.stderr)
        return 1
    finally:
        probe.close()

    elapsed = time.monotonic() - started
    min_interval = probe.min_observed_interval()
    payload |= {
        "probe_version": APP_VERSION,
        "endpoint": API_URL,
        "user_agent": USER_AGENT,
        "started_at": utc_now(),
        "elapsed_seconds": round(elapsed, 2),
        "requests_sent": probe.limiter.send_count,
        "limiter_min_interval_s": probe.limiter.min_interval_s,
        "observed_min_interval_s": round(min_interval, 3) if min_interval else None,
        "limiter_respected": min_interval is None or min_interval >= probe.limiter.min_interval_s - 0.05,
        "throttled": probe.throttled,
        "throttle_hits": probe.throttle_hits,
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }

    if not args.quiet:
        print(summarise(payload))
        print(
            f"\n共 {payload['requests_sent']} 次请求，用时 {payload['elapsed_seconds']} 秒；"
            f"最小起点间隔 {payload['observed_min_interval_s']} 秒"
            f"（限速器要求 ≥ {payload['limiter_min_interval_s']} 秒，"
            f"{'符合' if payload['limiter_respected'] else '不符合'}）。"
        )
        if payload["throttled"]:
            print(
                f"\n[警告] 本轮有 {len(payload['throttle_hits'])} 次请求被上游以 HTTP 406 空响应体拒绝"
                f"（请求序号 {payload['throttle_hits']}）。\n"
                "        406 这类空响应是上游的限流信号，不是查询语法错误；"
                "本轮结果不可作为契约结论。\n"
                f"        请冷却至少 10 分钟后用更大的 --interval（如 8）重跑。"
            )

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
        if not args.quiet:
            print(f"JSON 已写入 {args.json_out}")
    else:
        print(text)

    # 退出码：0 = 全部通过；1 = 有探测项失败；2 = 被限流，结果不可信
    if payload["throttled"]:
        return 2

    flat_ok = all(
        item.get("ok", True)
        for result in payload["results"]
        for item in (
            result.get("cases")
            or result.get("literals")
            or result.get("results")
            or [result]
        )
    )
    return 0 if flat_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
