"""`scripts/probe_arxiv_api.py` 的离线单测。

探测脚本是发布前契约验证的唯一工具，一旦它的判定逻辑写错，就会把
「上游坏了」误报成「一切正常」。这里用构造响应固定它的行为，不发任何网络请求。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO_ROOT / "scripts" / "probe_arxiv_api.py"

ATOM_OK = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">42</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>1</opensearch:itemsPerPage>
  <updated>2026-09-24T00:00:00Z</updated>
  <entry>
    <id>http://arxiv.org/abs/2101.00001v1</id>
    <title>Example</title>
  </entry>
</feed>
"""

ATOM_ERROR = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format</id>
    <title>Error</title>
    <summary>incorrect id format for 0</summary>
  </entry>
</feed>
"""

ATOM_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>0</opensearch:totalResults>
</feed>
"""

ENTITY_ATTACK = """<?xml version="1.0"?>
<!DOCTYPE feed [ <!ENTITY xxe SYSTEM "file:///c:/windows/win.ini"> ]>
<feed xmlns="http://www.w3.org/2005/Atom"><title>&xxe;</title></feed>
"""


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_probe_arxiv_api", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_probe_arxiv_api"] = module
    spec.loader.exec_module(module)
    return module


def make_response(
    status: int, content: bytes | str = b"", content_type: str | None = None, **headers: str
) -> httpx.Response:
    body = content.encode("utf-8") if isinstance(content, str) else content
    all_headers = dict(headers)
    if content_type:
        all_headers["content-type"] = content_type
    return httpx.Response(
        status,
        content=body,
        headers=all_headers,
        request=httpx.Request("GET", "https://export.arxiv.org/api/query"),
    )


# —— 敏感信息脱敏 ——


def test_redact_masks_url_credentials(probe: ModuleType) -> None:
    text = "proxy http://alice:s3cret@proxy.example:8080/path failed"
    out = probe.redact(text)
    assert "s3cret" not in out
    assert "alice" not in out
    assert "***@proxy.example" in out


def test_redact_masks_secret_keywords(probe: ModuleType) -> None:
    out = probe.redact("Authorization: Bearer abc123 api_key=zzz password=hunter2")
    assert "abc123" not in out
    assert "zzz" not in out
    assert "hunter2" not in out
    assert "***" in out


def test_sanitize_body_caps_at_one_kib(probe: ModuleType) -> None:
    out = probe.sanitize_body(b"x" * 50_000)
    assert len(out) <= probe.MAX_DIAGNOSTIC_BYTES


def test_sanitize_body_strips_control_characters(probe: ModuleType) -> None:
    out = probe.sanitize_body(b"a\x00b\x07c\x1bd\n\te")
    assert "\x00" not in out and "\x07" not in out and "\x1b" not in out
    assert "abcde" in out.replace(" ", "")


# —— classify ——


def test_classify_accepts_a_normal_atom_feed(probe: ModuleType) -> None:
    outcome = probe.classify(make_response(200, ATOM_OK, "application/atom+xml; charset=utf-8"))
    assert outcome["ok"] is True
    assert outcome["entry_count"] == 1
    assert outcome["total"] == "42"
    assert outcome["start_index"] == "0"
    assert outcome["feed_updated"] == "2026-09-24T00:00:00Z"
    assert "http://www.w3.org/2005/Atom" in outcome["namespaces_seen"]


def test_classify_flags_atom_error_entry(probe: ModuleType) -> None:
    outcome = probe.classify(make_response(200, ATOM_ERROR, "application/atom+xml"))
    assert outcome["ok"] is False
    assert outcome["atom_error"] is True
    assert outcome["error_entries"][0]["title"] == "Error"


def test_classify_accepts_zero_result_feed(probe: ModuleType) -> None:
    outcome = probe.classify(make_response(200, ATOM_EMPTY, "application/atom+xml"))
    assert outcome["ok"] is True
    assert outcome["entry_count"] == 0
    assert outcome["total"] == "0"


def test_classify_rejects_html_error_page(probe: ModuleType) -> None:
    outcome = probe.classify(make_response(503, "<html>nope</html>", "text/html"))
    assert outcome["ok"] is False
    assert outcome["reason"] == "non_xml_content_type"


def test_classify_reports_406_as_upstream_rejection(probe: ModuleType) -> None:
    """406 空响应体是实测到的边缘节点拒绝，不能当成查询语法错误。"""
    outcome = probe.classify(make_response(406, b""))
    assert outcome["ok"] is False
    assert outcome["reason"] == "http_406_not_acceptable"
    assert "上游" in outcome["note"] or "可重试" in outcome["note"]


def test_classify_records_cdn_headers(probe: ModuleType) -> None:
    outcome = probe.classify(
        make_response(406, b"", via="1.1 varnish", **{"x-cache": "MISS, MISS"})
    )
    assert outcome["headers"]["via"] == "1.1 varnish"
    assert outcome["headers"]["x-cache"] == "MISS, MISS"


def test_classify_rejects_malformed_xml(probe: ModuleType) -> None:
    outcome = probe.classify(make_response(200, "<feed><entry>", "application/atom+xml"))
    assert outcome["ok"] is False
    assert outcome["reason"] == "xml_parse_error"


def test_classify_rejects_entity_expansion(probe: ModuleType) -> None:
    """defusedxml 必须拦住 DOCTYPE/ENTITY 攻击。

    注意：`body_prefix` 是**原始字节**的脱敏前缀，因此仍然包含 DOCTYPE 里的
    文件名文本——这没有问题，我们要断言的是「没有被解析展开」：
    既没有 entry，也没有把文件内容读进来。
    """
    outcome = probe.classify(make_response(200, ENTITY_ATTACK, "application/atom+xml"))
    assert outcome["ok"] is False
    assert outcome["reason"] == "xml_parse_error"
    # 解析失败 → 不产出结构化字段，说明实体没有被展开
    assert "entry_count" not in outcome
    assert "total" not in outcome
    # 诊断前缀仍然受限，不会把正文整段回传
    assert len(outcome["body_prefix"]) <= probe.MAX_DIAGNOSTIC_BYTES


def test_classify_does_not_leak_expanded_entity_content(probe: ModuleType) -> None:
    """实体若被展开，注入的标记就会出现在结构化字段里；必须不出现。"""
    attack = """<?xml version="1.0"?>
<!DOCTYPE feed [ <!ENTITY xxe "INJECTED-MARKER"> ]>
<feed xmlns="http://www.w3.org/2005/Atom"><title>&xxe;</title></feed>
"""
    outcome = probe.classify(make_response(200, attack, "application/atom+xml"))
    assert outcome["ok"] is False
    assert "INJECTED-MARKER" not in str(outcome.get("namespaces_seen", []))
    assert "INJECTED-MARKER" not in str(outcome.get("total", ""))


def test_classify_redacts_credentials_in_url(probe: ModuleType) -> None:
    response = httpx.Response(
        200,
        content=ATOM_OK.encode(),
        headers={"content-type": "application/atom+xml"},
        request=httpx.Request("GET", "https://user:pw@export.arxiv.org/api/query"),
    )
    outcome = probe.classify(response)
    assert "pw" not in outcome["url"]
    assert "***@export.arxiv.org" in outcome["url"]


def test_first_entry_id_is_extracted(probe: ModuleType) -> None:
    assert probe._first_entry_id(make_response(200, ATOM_OK, "application/atom+xml")) == (
        "http://arxiv.org/abs/2101.00001v1"
    )
    assert probe._first_entry_id(make_response(200, "garbage", "application/atom+xml")) is None


# —— 限速器 ——


class FakeClock:
    """可推进的单调时钟，避免测试里真的 sleep。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_limiter_spaces_consecutive_requests(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(probe.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(probe.time, "sleep", clock.sleep)

    limiter = probe.MinIntervalLimiter(min_interval_s=3.1)
    starts: list[float] = []
    for _ in range(10):
        limiter.acquire()
        starts.append(clock.monotonic())

    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(gap >= 3.1 - 1e-6 for gap in gaps), gaps
    assert limiter.send_count == 10


def test_limiter_does_not_wait_when_enough_time_passed(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(probe.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(probe.time, "sleep", clock.sleep)

    limiter = probe.MinIntervalLimiter(min_interval_s=3.1)
    limiter.acquire()
    clock.now += 10.0
    waited = limiter.acquire()
    assert waited == 0.0
    assert clock.sleeps == []


def test_min_observed_interval_needs_two_requests(probe: ModuleType) -> None:
    client = probe.ProbeClient()
    try:
        assert client.min_observed_interval() is None
        client.send_starts[:] = [1.0, 5.0, 8.0]
        assert client.min_observed_interval() == 3.0
    finally:
        client.close()


# —— 表驱动用例 ——


def test_special_literals_are_non_empty_and_unique(probe: ModuleType) -> None:
    literals = probe.SPECIAL_LITERALS
    assert len(literals) == len(set(literals))
    assert all(literal.strip() for literal in literals)
    assert any("C++" in literal for literal in literals)


def test_total_stability_is_not_a_false_positive_when_both_requests_fail(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两次都失败时 total 都是 None——绝不能因此报「稳定」。"""
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)

    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return make_response(406, b"")

    client = probe.ProbeClient()
    try:
        monkeypatch.setattr(client.client, "request", fake_request)
        result = probe.probe_total_stability(client, "cat:cs.CL")
        assert result["stable"] is False
        assert result["ok"] is False
        assert result["compared"] is False
        assert result["note"]
    finally:
        client.close()


def test_total_stability_reports_stable_on_matching_totals(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return make_response(200, ATOM_OK, "application/atom+xml")

    client = probe.ProbeClient()
    try:
        monkeypatch.setattr(client.client, "request", fake_request)
        result = probe.probe_total_stability(client, "cat:cs.CL")
        assert result["stable"] is True
        assert result["ok"] is True
        assert result["compared"] is True
        assert result["first_total"] == result["second_total"] == "42"
    finally:
        client.close()


def test_every_mode_is_reachable(probe: ModuleType) -> None:
    assert "all" in probe.MODES
    for mode in probe.MODES:
        assert mode in probe.build_parser().format_help() or mode == "all"


def test_probe_interval_is_more_conservative_than_the_documented_floor(
    probe: ModuleType,
) -> None:
    """2026-09-24 实测：贴着 3 秒发请求会被上游以 406 限流，探测默认值必须更保守。"""
    assert probe.MIN_INTERVAL_S > probe.DOCUMENTED_INTERVAL_S
    assert probe.MIN_INTERVAL_S >= 4.0


def test_throttle_backoff_is_generous(probe: ModuleType) -> None:
    assert probe.THROTTLE_BACKOFF_S >= 30.0


def test_probe_client_records_406_as_throttling(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """406 必须被记成限流事件，而不是静默当成普通失败。"""
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)

    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return make_response(406, b"")

    client = probe.ProbeClient()
    try:
        monkeypatch.setattr(client.client, "request", fake_request)
        assert client.throttled is False
        client.get_api({"search_query": "cat:cs.CL", "max_results": 1})
        assert client.throttled is True
        assert client.throttle_hits == [1]
    finally:
        client.close()


def test_probe_client_not_throttled_on_success(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(*args: object, **kwargs: object) -> httpx.Response:
        return make_response(200, ATOM_OK, "application/atom+xml")

    client = probe.ProbeClient()
    try:
        monkeypatch.setattr(client.client, "request", fake_request)
        client.get_api({"search_query": "cat:cs.CL", "max_results": 1})
        assert client.throttled is False
        assert client.throttle_hits == []
    finally:
        client.close()
