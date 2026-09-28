"""T4.2 FastAPI contract tests; all upstream behavior is replaced with fakes."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import httpx
import pytest
import pytest_asyncio

from arxiv_filter.api import ApiServices, create_api_app
from arxiv_filter.api.context import BROWSER_HEARTBEAT_TTL_SECONDS
from arxiv_filter.api.ports import GeneratedFile
from arxiv_filter.api.schemas import ExportFormat, LogEvent, LogPage
from arxiv_filter.config import SettingsStore
from arxiv_filter.exporters import CompositeExporter
from arxiv_filter.arxiv.query_builder import build_query
from arxiv_filter.arxiv.provider import CancelToken
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.paper import Author, Paper, PaperView
from arxiv_filter.models.query import BuiltQuery, FeedPage, SearchRequest
from arxiv_filter.services.job_manager import JobManager
from arxiv_filter.services.search_service import SearchService
from arxiv_filter.storage.cache_repo import CacheRepository
from arxiv_filter.storage.database import Database
from arxiv_filter.storage.history_repo import HistoryRepository
from arxiv_filter.storage.read_repo import ReadRepository
from arxiv_filter.taxonomy import load_taxonomy

UTC = timezone.utc
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
TOKEN = "a" * 32


def make_paper(canonical_id: str, *, version: int = 1) -> Paper:
    return Paper(
        canonical_id=canonical_id,
        versioned_id=f"{canonical_id}v{version}",
        version=version,
        title=f"Paper {canonical_id}",
        authors=[Author(name="Example Author")],
        summary_raw="A test summary.",
        published_at=NOW - timedelta(days=1),
        updated_at=NOW,
        primary_category="cs.CL",
        categories=["cs.CL"],
        abs_url=f"https://arxiv.org/abs/{canonical_id}v{version}",
        retrieved_at=NOW,
    )


def make_request(*, result_cap: int = 20) -> dict[str, object]:
    return {
        "group_id": "cs",
        "category_id": "cs.CL",
        "start_date": "2026-09-17",
        "end_date": "2026-09-25",
        "keywords": '"language model"',
        "result_cap": result_cap,
    }


class FakeProvider:
    name = "arxiv_api"

    def __init__(self, papers: list[Paper]) -> None:
        self.papers = papers
        self.calls: list[tuple[int, int]] = []
        self.error: ArxivFilterError | None = None
        self.block_started: asyncio.Event | None = None
        self.block_release: asyncio.Event | None = None
        self.proxy_config: tuple[str | None, bool] | None = None

    async def reconfigure_proxy(self, proxy_url: str | None, *, trust_env: bool) -> None:
        self.proxy_config = (proxy_url, trust_env)

    async def fetch_page(
        self,
        query: BuiltQuery,
        start: int,
        limit: int,
        cancel: CancelToken,
    ) -> FeedPage:
        cancel.raise_if_cancelled()
        self.calls.append((start, limit))
        if self.block_started is not None and self.block_release is not None:
            self.block_started.set()
            await self.block_release.wait()
            cancel.raise_if_cancelled()
        if self.error is not None:
            raise self.error
        page = self.papers[start : start + limit]
        return FeedPage(
            total_results=len(self.papers),
            start_index=start,
            items_per_page=len(page),
            papers=page,
        )


class FakeExporter:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], ExportFormat, bool]] = []
        self.filename = "papers"
        self.failure: Exception | None = None

    async def export(
        self,
        papers: Sequence[PaperView],
        *,
        file_format: ExportFormat,
        query: str,
        include_state: bool,
        exported_at: datetime,
        request: SearchRequest | None = None,
    ) -> GeneratedFile:
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            (tuple(paper.canonical_id for paper in papers), file_format, include_state)
        )
        return GeneratedFile(
            b"exported", f"{self.filename}.{file_format}", "application/octet-stream"
        )


class FakeLogReader:
    async def list_events(self, *, job_id, cursor, limit) -> LogPage:
        return LogPage(
            items=[
                LogEvent(
                    timestamp=NOW,
                    level="INFO",
                    event="network request",
                    job_id=str(job_id) if job_id else None,
                    details={
                        "token": "do-not-leak",
                        "note": "Authorization: Bearer abc123",
                        "nested": [{"password": "hidden"}, "safe text"],
                        "count": 2,
                    },
                )
            ]
        )


@pytest_asyncio.fixture
async def api_fixture(tmp_path: Path):
    db = Database(tmp_path / "api.db")
    await db.open()
    taxonomy = load_taxonomy()
    reads = ReadRepository(db)
    history = HistoryRepository(db)
    cache = CacheRepository(db)
    provider = FakeProvider([make_paper("2609.0001"), make_paper("2609.0002")])
    search = SearchService(
        cache,
        history,
        provider,
        taxonomy=taxonomy,
        clock=lambda: NOW,
    )
    manager = JobManager(search, read_repository=reads)
    exporter = FakeExporter()
    services = ApiServices(
        taxonomy=taxonomy,
        jobs=manager,
        read_repository=reads,
        history_repository=history,
        cache_repository=cache,
        auth_token=TOKEN,
        clock=lambda: NOW,
        log_reader=FakeLogReader(),
        exporter=exporter,
    )
    app = create_api_app(services)
    yield app, services, provider, exporter
    await db.close()


def auth_headers() -> dict[str, str]:
    return {"X-App-Token": TOKEN}


async def open_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def assert_error_envelope(response: httpx.Response, status: int, code: ErrorCode) -> None:
    assert response.status_code == status
    body = response.json()
    assert body["error"]["code"] == str(code)
    assert body["error"]["message_zh"]
    assert body["error"]["request_id"]
    assert response.headers["X-Request-ID"] == body["error"]["request_id"]


@pytest.mark.asyncio
async def test_search_categories_results_seen_read_history_logs_and_export(api_fixture) -> None:
    app, services, _provider, exporter = api_fixture
    await services.read_repository.mark_seen(["2609.0001"], NOW - timedelta(days=2))
    await services.read_repository.set_read("2609.0001", True, NOW - timedelta(days=1))

    async with await open_client(app) as client:
        categories = await client.get("/api/v1/categories", headers=auth_headers())
        assert categories.status_code == 200
        assert categories.json()["taxonomy_version"] == services.taxonomy.taxonomy_version
        assert any(group["id"] == "cs" for group in categories.json()["groups"])

        created = await client.post(
            "/api/v1/search-jobs",
            headers=auth_headers(),
            json=make_request(),
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        await services.jobs.wait(job_id)

        status = await client.get(f"/api/v1/search-jobs/{job_id}", headers=auth_headers())
        assert status.status_code == 200
        assert status.json()["job"]["status"]["phase"] == "completed"
        assert status.json()["error"] is None

        # Mutations after job creation must not change its frozen read-state view.
        await services.read_repository.mark_seen(["2609.0002"], NOW)
        await services.read_repository.set_read("2609.0002", True, NOW)
        all_results = await client.get(
            f"/api/v1/search-jobs/{job_id}/results?limit=10",
            headers=auth_headers(),
        )
        assert all_results.status_code == 200
        papers = {item["canonical_id"]: item for item in all_results.json()["items"]}
        assert papers["2609.0001"]["is_read"] is True
        assert papers["2609.0001"]["is_new"] is False
        assert papers["2609.0002"]["is_read"] is False
        assert papers["2609.0002"]["is_new"] is True

        only_new = await client.get(
            f"/api/v1/search-jobs/{job_id}/results?read_filter=new",
            headers=auth_headers(),
        )
        assert [item["canonical_id"] for item in only_new.json()["items"]] == ["2609.0002"]

        only_unread = await client.get(
            f"/api/v1/search-jobs/{job_id}/results?read_filter=unread",
            headers=auth_headers(),
        )
        assert only_unread.json()["items"] == []

        seen = await client.post(
            "/api/v1/seen",
            headers=auth_headers(),
            json={"job_id": job_id, "paper_ids": ["2609.0002v1"]},
        )
        assert seen.status_code == 200
        assert seen.json()["updated"] == 1

        not_in_job = await client.post(
            "/api/v1/seen",
            headers=auth_headers(),
            json={"job_id": job_id, "paper_ids": ["2609.9999"]},
        )
        assert_error_envelope(not_in_job, 422, ErrorCode.PAPER_NOT_IN_JOB)

        read_state = await client.put(
            "/api/v1/read-state/2609.0002v3",
            headers=auth_headers(),
            json={"read": True},
        )
        assert read_state.status_code == 200
        assert read_state.json()["canonical_id"] == "2609.0002"
        assert read_state.json()["read_at"] == "2026-09-25T12:00:00Z"

        legacy = await client.put(
            "/api/v1/read-state/hep-th/9901001",
            headers=auth_headers(),
            json={"read": False},
        )
        assert legacy.status_code == 200
        assert legacy.json()["canonical_id"] == "hep-th/9901001"

        history = await client.get("/api/v1/history?limit=10", headers=auth_headers())
        assert history.status_code == 200
        assert history.json()["items"][0]["status"] == "completed"

        logs = await client.get("/api/v1/logs", headers=auth_headers())
        assert logs.status_code == 200
        log_body = logs.json()["items"][0]
        assert log_body["details"]["token"] == "***"
        assert "abc123" not in log_body["details"]["note"]
        assert log_body["details"]["nested"] == [{"password": "***"}, "safe text"]
        assert log_body["details"]["count"] == 2

        exported = await client.post(
            "/api/v1/exports",
            headers=auth_headers(),
            json={
                "job_id": job_id,
                "format": "csv",
                "paper_ids": ["2609.0002"],
                "include_state": True,
            },
        )
        assert exported.status_code == 200
        assert exported.content == b"exported"
        assert "papers.csv" in exported.headers["content-disposition"]
        assert exporter.calls == [(("2609.0002",), "csv", True)]

        cleared = await client.request(
            "DELETE", "/api/v1/cache", headers=auth_headers(), json={"confirm": True}
        )
        assert cleared.status_code == 200
        assert cleared.json()["removed_entries"] >= 1
        assert await services.history_repository.count() == 1


@pytest.mark.asyncio
async def test_api_errors_have_stable_envelopes_and_token_is_required(api_fixture) -> None:
    app, _services, _provider, _exporter = api_fixture
    async with await open_client(app) as client:
        missing_token = await client.get("/api/v1/categories")
        assert_error_envelope(missing_token, 403, ErrorCode.INVALID_TOKEN)

        invalid_request = await client.post(
            "/api/v1/search-jobs",
            headers=auth_headers(),
            json=make_request(result_cap=3000),
        )
        assert_error_envelope(invalid_request, 422, ErrorCode.INVALID_SEARCH_REQUEST)
        assert "input" not in str(invalid_request.json()["error"]["details"])

        missing_route = await client.get("/api/v1/not-a-route", headers=auth_headers())
        assert_error_envelope(missing_route, 404, ErrorCode.API_NOT_FOUND)

        wrong_method = await client.put("/api/v1/categories", headers=auth_headers())
        assert_error_envelope(wrong_method, 405, ErrorCode.API_METHOD_NOT_ALLOWED)

        missing_job = await client.get(
            "/api/v1/search-jobs/00000000-0000-0000-0000-000000000001",
            headers=auth_headers(),
        )
        assert_error_envelope(missing_job, 404, ErrorCode.JOB_NOT_FOUND)

        no_confirm = await client.request(
            "DELETE", "/api/v1/cache", headers=auth_headers(), json={"confirm": False}
        )
        assert_error_envelope(
            no_confirm, 400, ErrorCode.CACHE_CLEAR_CONFIRMATION_REQUIRED
        )

        created = await client.post(
            "/api/v1/search-jobs",
            headers=auth_headers(),
            json=make_request(),
        )
        job_id = created.json()["job_id"]
        await _services.jobs.wait(job_id)

        cannot_continue = await client.post(
            f"/api/v1/search-jobs/{job_id}/continue",
            headers=auth_headers(),
            json={"additional_results": 100},
        )
        assert_error_envelope(
            cannot_continue, 409, ErrorCode.JOB_INVALID_TRANSITION
        )

        cannot_cancel = await client.post(
            f"/api/v1/search-jobs/{job_id}/cancel",
            headers=auth_headers(),
        )
        assert_error_envelope(cannot_cancel, 409, ErrorCode.JOB_ALREADY_FINISHED)

        _services.exporter = None
        unavailable_exporter = await client.post(
            "/api/v1/exports",
            headers=auth_headers(),
            json={"job_id": job_id, "format": "json"},
        )
        assert_error_envelope(unavailable_exporter, 501, ErrorCode.EXPORT_NOT_AVAILABLE)


@pytest.mark.asyncio
async def test_system_health_bootstrap_origin_check_heartbeat_and_shutdown(api_fixture) -> None:
    app, services, _provider, _exporter = api_fixture
    services.allowed_origin = "http://127.0.0.1:43123"
    services.launcher_challenge = "c" * 32

    async with await open_client(app) as client:
        simple_health = await client.get("/health")
        assert simple_health.status_code == 200
        assert simple_health.json()["status"] == "ok"

        health = await client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["session_token"] is None

        bootstrap = await client.get(
            f"/api/v1/system/health?challenge={services.launcher_challenge}"
        )
        assert bootstrap.json()["challenge_valid"] is True
        assert bootstrap.json()["session_token"] == TOKEN

        rejected_origin = await client.get(
            "/api/v1/categories",
            headers={**auth_headers(), "Origin": "https://untrusted.example"},
        )
        assert_error_envelope(rejected_origin, 403, ErrorCode.ORIGIN_REJECTED)

        heartbeat = await client.post(
            "/api/v1/system/heartbeat",
            headers={**auth_headers(), "Origin": services.allowed_origin},
            json={"client_id": "1" * 32, "active": True},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["accepted"] is True
        assert "1" * 32 in services.active_browser_clients

        closed = await client.post(
            "/api/v1/system/heartbeat",
            headers={**auth_headers(), "Origin": services.allowed_origin},
            json={"client_id": "1" * 32, "active": False},
        )
        assert closed.status_code == 200
        assert not services.active_browser_clients
        assert services.no_browser_since_monotonic is not None

        services.update_browser_presence("2" * 32, active=True, now=100.0)
        services.prune_browser_presence(now=100.0 + BROWSER_HEARTBEAT_TTL_SECONDS + 1)
        assert not services.active_browser_clients
        assert services.no_browser_since_monotonic == (
            100.0 + BROWSER_HEARTBEAT_TTL_SECONDS + 1
        )

        shutdown = await client.post(
            "/api/v1/system/shutdown",
            headers={**auth_headers(), "Origin": services.allowed_origin},
            json={},
        )
        assert shutdown.status_code == 202
        assert shutdown.json()["status"] == "shutting_down"

        unauthorized_shutdown = await client.post(
            "/api/v1/system/shutdown", json={}
        )
        assert_error_envelope(unauthorized_shutdown, 403, ErrorCode.INVALID_TOKEN)


@pytest.mark.asyncio
async def test_continue_creates_a_bounded_child_job_for_truncated_result(api_fixture) -> None:
    app, services, provider, _exporter = api_fixture
    async with await open_client(app) as client:
        created = await client.post(
            "/api/v1/search-jobs",
            headers=auth_headers(),
            json=make_request(result_cap=1),
        )
        job_id = created.json()["job_id"]
        first = await services.jobs.wait(job_id)
        assert first.truncated is True

        continued = await client.post(
            f"/api/v1/search-jobs/{job_id}/continue",
            headers=auth_headers(),
            json={"additional_results": 1},
        )
        assert continued.status_code == 202
        next_id = continued.json()["job_id"]
        assert continued.json()["source_job_id"] == job_id
        final = await services.jobs.wait(next_id)
        assert final.status.phase.value == "completed"
        assert len(provider.calls) == 2

        history = await services.history_repository.list(limit=10)
        assert history[0].request["result_cap"] == 2

        first_page = await client.get(
            "/api/v1/history?limit=1",
            headers=auth_headers(),
        )
        assert first_page.status_code == 200
        assert first_page.json()["next_cursor"] is not None
        next_page = await client.get(
            f"/api/v1/history?limit=1&cursor={first_page.json()['next_cursor']}",
            headers=auth_headers(),
        )
        assert next_page.status_code == 200
        assert len(next_page.json()["items"]) == 1


@pytest.mark.asyncio
async def test_stale_confirmation_is_reported_and_only_explicit_consent_returns_papers(
    api_fixture,
) -> None:
    app, services, provider, _exporter = api_fixture
    request = SearchRequest.model_validate(make_request())
    query = build_query(request, taxonomy=services.taxonomy)
    await services.cache_repository.put_complete(
        query.cache_key,
        query.canonical_json,
        [make_paper("2609.0001")],
        now=NOW - timedelta(hours=7),
    )
    provider.error = ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE)

    async with await open_client(app) as client:
        failed = await client.post(
            "/api/v1/search-jobs",
            headers=auth_headers(),
            json=make_request(),
        )
        failed_id = failed.json()["job_id"]
        await services.jobs.wait(failed_id)

        status = await client.get(
            f"/api/v1/search-jobs/{failed_id}",
            headers=auth_headers(),
        )
        assert status.status_code == 200
        assert status.json()["error"]["code"] == str(ErrorCode.ARXIV_NOT_ACCEPTABLE)
        assert status.json()["stale_cache"]["result_count"] == 1
        assert status.json()["stale_cache"]["created_at"]
        assert status.json()["stale_cache"]["expires_at"]

        failed_results = await client.get(
            f"/api/v1/search-jobs/{failed_id}/results",
            headers=auth_headers(),
        )
        assert_error_envelope(failed_results, 503, ErrorCode.ARXIV_NOT_ACCEPTABLE)

        consented_body = {**make_request(), "allow_stale_cache": True}
        consented = await client.post(
            "/api/v1/search-jobs",
            headers=auth_headers(),
            json=consented_body,
        )
        consented_id = consented.json()["job_id"]
        await services.jobs.wait(consented_id)
        status = await client.get(
            f"/api/v1/search-jobs/{consented_id}",
            headers=auth_headers(),
        )
        assert status.json()["error"] is None
        assert status.json()["stale_cache"] is None
        results = await client.get(
            f"/api/v1/search-jobs/{consented_id}/results",
            headers=auth_headers(),
        )
        assert [item["canonical_id"] for item in results.json()["items"]] == [
            "2609.0001"
        ]


@pytest.mark.asyncio
async def test_invalid_ids_filters_and_export_guardrails(api_fixture) -> None:
    app, services, _provider, exporter = api_fixture
    await services.read_repository.set_read("2609.0001", True, NOW)

    async with await open_client(app) as client:
        created = await client.post(
            "/api/v1/search-jobs",
            headers=auth_headers(),
            json=make_request(),
        )
        job_id = created.json()["job_id"]
        await services.jobs.wait(job_id)

        unread = await client.get(
            f"/api/v1/search-jobs/{job_id}/results?read_filter=unread",
            headers=auth_headers(),
        )
        assert [item["canonical_id"] for item in unread.json()["items"]] == [
            "2609.0002"
        ]
        empty_page = await client.get(
            f"/api/v1/search-jobs/{job_id}/results?offset=10",
            headers=auth_headers(),
        )
        assert empty_page.json()["items"] == []

        invalid_seen_id = await client.post(
            "/api/v1/seen",
            headers=auth_headers(),
            json={"job_id": job_id, "paper_ids": ["not-an-arxiv-id"]},
        )
        assert_error_envelope(invalid_seen_id, 422, ErrorCode.INVALID_ARXIV_ID)

        invalid_read_id = await client.put(
            "/api/v1/read-state/not-an-arxiv-id",
            headers=auth_headers(),
            json={"read": True},
        )
        assert_error_envelope(invalid_read_id, 422, ErrorCode.INVALID_ARXIV_ID)

        invalid_cursor = await client.get(
            f"/api/v1/history?cursor={'z' * 36}",
            headers=auth_headers(),
        )
        assert_error_envelope(
            invalid_cursor, 422, ErrorCode.INVALID_SEARCH_REQUEST
        )

        invalid_subset = await client.post(
            "/api/v1/exports",
            headers=auth_headers(),
            json={"job_id": job_id, "format": "json", "paper_ids": ["2609.9999"]},
        )
        assert_error_envelope(invalid_subset, 422, ErrorCode.EXPORT_INVALID_SUBSET)

        exporter.filename = "../unsafe"
        unsafe_filename = await client.post(
            "/api/v1/exports",
            headers=auth_headers(),
            json={"job_id": job_id, "format": "json"},
        )
        assert_error_envelope(unsafe_filename, 500, ErrorCode.EXPORT_FAILED)

        exporter.filename = "papers"
        exporter.failure = RuntimeError("private exporter traceback")
        failed_export = await client.post(
            "/api/v1/exports",
            headers=auth_headers(),
            json={"job_id": job_id, "format": "json"},
        )
        assert_error_envelope(failed_export, 500, ErrorCode.EXPORT_FAILED)
        assert "private exporter traceback" not in failed_export.text


@pytest.mark.asyncio
async def test_active_job_blocks_cache_clear_and_cancelled_export_requires_consent(
    api_fixture,
) -> None:
    app, services, provider, _exporter = api_fixture
    provider.block_started = asyncio.Event()
    provider.block_release = asyncio.Event()

    async with await open_client(app) as client:
        created = await client.post(
            "/api/v1/search-jobs",
            headers=auth_headers(),
            json=make_request(),
        )
        job_id = created.json()["job_id"]
        await provider.block_started.wait()

        blocked_clear = await client.request(
            "DELETE",
            "/api/v1/cache",
            headers=auth_headers(),
            json={"confirm": True},
        )
        assert_error_envelope(blocked_clear, 409, ErrorCode.JOB_LIMIT_REACHED)

        blocked_shutdown = await client.post(
            "/api/v1/system/shutdown",
            headers=auth_headers(),
            json={},
        )
        assert_error_envelope(blocked_shutdown, 409, ErrorCode.SHUTDOWN_BLOCKED)

        confirmed_shutdown = await client.post(
            "/api/v1/system/shutdown",
            headers=auth_headers(),
            json={"confirm_active": True},
        )
        assert confirmed_shutdown.status_code == 202

        cancelled = await client.post(
            f"/api/v1/search-jobs/{job_id}/cancel",
            headers=auth_headers(),
        )
        assert cancelled.status_code == 202
        provider.block_release.set()
        final = await services.jobs.wait(job_id)
        assert final.status.phase.value == "cancelled"

        repeated_cancel = await client.post(
            f"/api/v1/search-jobs/{job_id}/cancel",
            headers=auth_headers(),
        )
        assert repeated_cancel.status_code == 202
        assert repeated_cancel.json()["status"]["phase"] == "cancelled"

        no_consent = await client.post(
            "/api/v1/exports",
            headers=auth_headers(),
            json={"job_id": job_id, "format": "json"},
        )
        assert_error_envelope(
            no_consent, 409, ErrorCode.EXPORT_PARTIAL_CONFIRMATION_REQUIRED
        )
        no_execution = await client.post(
            "/api/v1/exports",
            headers=auth_headers(),
            json={"job_id": job_id, "format": "json", "confirm_partial": True},
        )
        assert_error_envelope(no_execution, 422, ErrorCode.EXPORT_INVALID_SUBSET)


@pytest.mark.asyncio
async def test_api_rejects_naive_clock_and_sanitizes_unexpected_failures(api_fixture) -> None:
    app, services, _provider, _exporter = api_fixture
    services.clock = lambda: datetime(2026, 9, 25, 12, 0)
    async with await open_client(app) as client:
        invalid_clock = await client.put(
            "/api/v1/read-state/2609.0001",
            headers=auth_headers(),
            json={"read": True},
        )
        assert_error_envelope(invalid_clock, 500, ErrorCode.INTERNAL_ERROR)

    async def fail_logs(*, job_id, cursor, limit) -> LogPage:
        raise RuntimeError("secret internal detail")

    services.log_reader.list_events = fail_logs
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/logs", headers=auth_headers())
    assert_error_envelope(response, 500, ErrorCode.INTERNAL_ERROR)
    assert "secret internal detail" not in response.text


@pytest.mark.asyncio
async def test_settings_mask_proxy_credentials_and_reconfigure_provider(api_fixture, tmp_path: Path) -> None:
    app, services, provider, _exporter = api_fixture
    settings_path = tmp_path / "config.json"
    services.settings_store = SettingsStore(settings_path)
    services.provider = provider
    async with await open_client(app) as client:
        initial = await client.get("/api/v1/system/settings", headers=auth_headers())
        assert initial.status_code == 200
        assert initial.json()["proxy_mode"] == "system"
        saved = await client.put(
            "/api/v1/system/settings",
            headers=auth_headers(),
            json={
                "proxy_mode": "custom",
                "proxy_url": "http://reader:secret-password@proxy.example:8123/private?token=should-not-return",
                "open_marks_read": True,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["proxy_display"] == "http://***@proxy.example:8123"
        assert "secret-password" not in saved.text
        assert "should-not-return" not in saved.text
        assert provider.proxy_config == (
            "http://reader:secret-password@proxy.example:8123/private?token=should-not-return",
            False,
        )
        assert json.loads(settings_path.read_text(encoding="utf-8"))["proxy_url"].startswith("http://reader:")

        invalid = await client.put(
            "/api/v1/system/settings",
            headers=auth_headers(),
            json={"proxy_mode": "custom", "proxy_url": "ftp://proxy.example", "open_marks_read": False},
        )
        assert_error_envelope(invalid, 422, ErrorCode.SETTINGS_UPDATE_FAILED)


@pytest.mark.asyncio
async def test_settings_cannot_change_proxy_during_active_search(api_fixture, tmp_path: Path) -> None:
    app, services, provider, _exporter = api_fixture
    path = tmp_path / "config.json"
    services.settings_store = SettingsStore(path)
    services.provider = provider
    provider.block_started = asyncio.Event()
    provider.block_release = asyncio.Event()
    async with await open_client(app) as client:
        created = await client.post("/api/v1/search-jobs", headers=auth_headers(), json=make_request())
        job_id = created.json()["job_id"]
        await provider.block_started.wait()
        response = await client.put(
            "/api/v1/system/settings",
            headers=auth_headers(),
            json={"proxy_mode": "none", "open_marks_read": False},
        )
        assert_error_envelope(response, 409, ErrorCode.JOB_LIMIT_REACHED)
        assert not path.exists()
        assert provider.proxy_config is None
        await client.post(f"/api/v1/search-jobs/{job_id}/cancel", headers=auth_headers())
        provider.block_release.set()
        await services.jobs.wait(job_id)


@pytest.mark.asyncio
async def test_real_exporter_streams_all_formats_and_only_selected_job_subset(api_fixture) -> None:
    app, services, _provider, _exporter = api_fixture
    services.exporter = CompositeExporter()
    async with await open_client(app) as client:
        created = await client.post(
            "/api/v1/search-jobs", headers=auth_headers(), json=make_request()
        )
        job_id = created.json()["job_id"]
        await services.jobs.wait(job_id)
        for file_format, extension in (
            ("csv", "csv"), ("json", "json"), ("markdown", "md"),
            ("xlsx", "xlsx"), ("bibtex", "bib"),
        ):
            response = await client.post(
                "/api/v1/exports",
                headers=auth_headers(),
                json={
                    "job_id": job_id,
                    "format": file_format,
                    "paper_ids": ["2609.0001"],
                    "include_state": True,
                },
            )
            assert response.status_code == 200
            assert response.content
            assert f". {extension}" not in response.headers["content-disposition"]
            assert f".{extension}" in response.headers["content-disposition"]
            assert "cs.CL_20260917-20260925" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_openapi_contract_exposes_all_v1_operations(api_fixture) -> None:
    app, _services, _provider, _exporter = api_fixture
    schema = app.openapi()
    contract_path = Path(__file__).parents[1] / "fixtures" / "openapi-v1-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    actual = sorted(
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method in operations
    )
    assert actual == contract["operations"]
    security_schemes = schema["components"]["securitySchemes"]
    assert security_schemes["APIKeyHeader"]["name"] == contract["authenticated_header"]
    error_schema = schema["components"]["schemas"]["ErrorPayload"]
    assert sorted(error_schema["required"]) == sorted(contract["error_schema_required"])
    assert "SearchRequest" in schema["components"]["schemas"]
    for operations in schema["paths"].values():
        for operation in operations.values():
            for status in (
                "400",
                "401",
                "403",
                "404",
                "405",
                "409",
                "422",
                "500",
                "501",
                "502",
                "503",
                "504",
            ):
                error_response = operation["responses"][status]
                assert (
                    error_response["content"]["application/json"]["schema"]["$ref"]
                    == "#/components/schemas/ErrorEnvelope"
                )
