"""脱敏与诊断限长（方案 §4.2「日志」行、§5.4）。

这是安全相关代码：漏掉一个凭据就可能出现在用户随手复制的诊断里。
"""

from __future__ import annotations

import pytest

from arxiv_filter.redaction import (
    MAX_DIAGNOSTIC_BYTES,
    diagnostic_prefix,
    redact,
    redact_url,
    summarise_proxy,
)


# —— URL 凭据 ——


@pytest.mark.parametrize(
    "raw",
    [
        "http://alice:s3cret@proxy.corp:8080/path",
        "https://bob:hunter2@proxy.example/",
        "socks5://user:pass@127.0.0.1:1080",
    ],
)
def test_url_credentials_are_masked(raw: str) -> None:
    out = redact(raw)
    assert "s3cret" not in out
    assert "hunter2" not in out
    assert "alice:" not in out
    assert "bob:" not in out
    assert "user:pass" not in out
    assert "***@" in out


def test_redact_url_keeps_the_host_visible() -> None:
    """主机名要保留——否则用户无法判断自己配的是哪个代理。"""
    out = redact_url("http://alice:s3cret@proxy.corp:8080/path")
    assert out == "http://***@proxy.corp:8080/path"


def test_url_without_credentials_is_unchanged() -> None:
    url = "https://export.arxiv.org/api/query?search_query=cat:cs.CL"
    assert redact_url(url) == url


# —— 键值型凭据 ——


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer abc123",
        "authorization: Basic dXNlcjpwYXNz",
        "api_key=zzz-999",
        "API-KEY: qqq",
        "access_token=ttt",
        "refresh_token=rrr",
        "token: xxx",
        "password=hunter2",
        "passwd = ppp",
        "pwd: www",
        "secret=sss",
        "client_secret=ccc",
    ],
)
def test_secret_key_values_are_masked_including_the_value(raw: str) -> None:
    """必须连**值**一起遮蔽。

    只替换键名会留下凭据明文——早期实现就是这样，测试里当场抓到了。
    """
    out = redact(raw)
    for leaked in ("abc123", "dXNlcjpwYXNz", "zzz-999", "qqq", "ttt", "rrr", "xxx",
                   "hunter2", "ppp", "www", "sss", "ccc"):
        assert leaked not in out, f"{leaked!r} 泄漏在 {out!r} 里"
    assert "***" in out


def test_redaction_is_case_insensitive() -> None:
    assert "s3cret" not in redact("PASSWORD=s3cret")
    assert "s3cret" not in redact("PaSsWoRd=s3cret")


def test_non_secret_text_is_preserved() -> None:
    text = "connection refused after 4 attempts"
    assert redact(text) == text


def test_word_containing_token_is_not_mistaken_for_a_secret() -> None:
    """``tokenizer`` 不是 ``token``——不能把普通词误伤成 ``***``。"""
    assert redact("the tokenizer failed") == "the tokenizer failed"


def test_proxy_summary_is_none_safe() -> None:
    assert summarise_proxy(None) is None
    assert summarise_proxy("http://a:b@host:1") == "http://***@host:1"


# —— 诊断前缀 ——


def test_diagnostic_prefix_is_bounded() -> None:
    out = diagnostic_prefix(b"x" * 100_000)
    assert len(out) <= MAX_DIAGNOSTIC_BYTES


def test_diagnostic_prefix_strips_control_characters() -> None:
    out = diagnostic_prefix(b"a\x00b\x07c\x1bd\x7fe")
    assert "\x00" not in out and "\x07" not in out and "\x1b" not in out
    assert "abcde" in out.replace(" ", "")


def test_diagnostic_prefix_collapses_whitespace_to_one_line() -> None:
    out = diagnostic_prefix(b"line one\n\n\tline two")
    assert "\n" not in out and "\t" not in out
    assert out == "line one line two"


def test_diagnostic_prefix_redacts_credentials() -> None:
    out = diagnostic_prefix(b"proxy http://alice:s3cret@host failed password=hunter2")
    assert "s3cret" not in out
    assert "hunter2" not in out
    assert "***" in out


def test_diagnostic_prefix_normalises_fullwidth() -> None:
    out = diagnostic_prefix("ｈｔｔｐ：／／ａｂｃ".encode())
    assert "ｈｔｔｐ" not in out


def test_diagnostic_prefix_accepts_str_and_bytes() -> None:
    assert diagnostic_prefix("hello") == diagnostic_prefix(b"hello")


def test_diagnostic_prefix_handles_invalid_utf8() -> None:
    out = diagnostic_prefix(b"\xff\xfe valid ascii")
    assert "valid ascii" in out


def test_diagnostic_prefix_empty_input() -> None:
    assert diagnostic_prefix(b"") == ""


def test_diagnostic_prefix_respects_a_custom_limit() -> None:
    out = diagnostic_prefix(b"y" * 5000, limit=64)
    assert len(out) <= 64
