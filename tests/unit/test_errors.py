"""错误码、中文文案与统一错误信封。"""

from __future__ import annotations

import pytest

from arxiv_filter.errors import (
    ERROR_SPECS,
    ArxivFilterError,
    Diagnostic,
    ErrorCode,
)


def test_every_error_code_has_a_spec() -> None:
    missing = [code for code in ErrorCode if code not in ERROR_SPECS]
    assert missing == [], f"缺少 ErrorSpec 的错误码：{missing}"


def test_specs_have_bilingual_messages_and_valid_status() -> None:
    for code, spec in ERROR_SPECS.items():
        assert spec.message_zh.strip(), f"{code} 缺少中文文案"
        assert spec.message_en.strip(), f"{code} 缺少英文文案"
        assert 400 <= spec.http_status <= 599, f"{code} 的 http_status 非法"
        # 中文文案面向非技术用户：不应出现裸异常类名或 traceback 痕迹
        assert "Traceback" not in spec.message_zh
        assert "Exception" not in spec.message_zh


def test_code_string_matches_enum_value() -> None:
    for code in ErrorCode:
        assert code.value == code.name


def test_arxiv_filter_error_exposes_status_and_retryable() -> None:
    exc = ArxivFilterError(ErrorCode.ARXIV_TIMEOUT)
    assert exc.http_status == 504
    assert exc.retryable is True
    assert "超时" in exc.message_zh


def test_custom_message_overrides_spec() -> None:
    exc = ArxivFilterError(ErrorCode.INTERNAL_ERROR, message_zh="自定义提示")
    assert exc.message_zh == "自定义提示"
    # 英文与状态码仍来自 spec
    assert exc.spec.message_en
    assert exc.http_status == 500


def test_details_are_copied_not_aliased() -> None:
    source = {"response_bytes": 4096}
    exc = ArxivFilterError(ErrorCode.ARXIV_BAD_RESPONSE, details=source)
    source["response_bytes"] = 0
    assert exc.details == {"response_bytes": 4096}


def test_envelope_shape_matches_contract() -> None:
    exc = ArxivFilterError(ErrorCode.ARXIV_TIMEOUT, details={"attempts": 4})
    envelope = exc.to_envelope("01JABCDEF")
    assert set(envelope) == {"error"}
    error = envelope["error"]
    assert set(error) == {
        "code",
        "message_zh",
        "message_en",
        "retryable",
        "request_id",
        "details",
    }
    assert error["code"] == "ARXIV_TIMEOUT"
    assert error["request_id"] == "01JABCDEF"
    assert error["details"] == {"attempts": 4}


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        (ErrorCode.JOB_NOT_FOUND, 404),
        (ErrorCode.INVALID_SEARCH_REQUEST, 422),
        (ErrorCode.INVALID_TOKEN, 403),
        (ErrorCode.SHUTDOWN_BLOCKED, 409),
        (ErrorCode.DATABASE_TOO_NEW, 500),
    ],
)
def test_status_mapping_matches_route_contract(code: ErrorCode, expected_status: int) -> None:
    assert ArxivFilterError(code).http_status == expected_status


def test_diagnostic_defaults_are_empty() -> None:
    diag = Diagnostic()
    assert diag.status is None
    assert diag.content_type == ""
    assert diag.body_prefix == ""
    assert diag.retry_after is None
    assert diag.extra == {}


def test_diagnostic_extra_is_not_shared_between_instances() -> None:
    first = Diagnostic()
    second = Diagnostic()
    first.extra["a"] = 1
    assert second.extra == {}
