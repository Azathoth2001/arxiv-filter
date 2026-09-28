"""T4.3 application service assembly stays local and resource cleanup is complete."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import webbrowser

import httpx
import pytest

from arxiv_filter import paths
from arxiv_filter.application import build_runtime_resources
from arxiv_filter.launcher import _open_existing_instance
from arxiv_filter.runtime import RuntimeRecord, bind_loopback, write_runtime


@pytest.mark.asyncio
async def test_runtime_resources_open_and_close_sqlite_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "runtime.db"
    backup_path = tmp_path / "backup"
    monkeypatch.setattr(paths, "db_path", lambda: database_path)
    monkeypatch.setattr(paths, "backup_dir", lambda: backup_path)
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "config.json")

    resources = await build_runtime_resources(
        port=43123,
        auth_token="a" * 32,
        challenge="b" * 32,
        shutdown_signal=asyncio.Event(),
    )
    try:
        assert resources.database.is_open
        assert resources.database.schema_version == 1
        assert resources.services.allowed_origin == "http://127.0.0.1:43123"
        assert resources.services.launcher_challenge == "b" * 32
        assert resources.services.exporter is not None
    finally:
        await resources.close()
    assert not resources.database.is_open


@pytest.mark.asyncio
async def test_live_server_binds_loopback_serves_health_and_shuts_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arxiv_filter.application as application
    from arxiv_filter.application import serve_application

    monkeypatch.setattr(paths, "db_path", lambda: tmp_path / "server.db")
    monkeypatch.setattr(paths, "backup_dir", lambda: tmp_path / "backup")
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(application, "BROWSER_CLOSE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(application, "HEARTBEAT_POLL_SECONDS", 0.01)
    listener = bind_loopback(0)
    port = int(listener.getsockname()[1])
    started = asyncio.Event()
    runtime_file = tmp_path / "runtime.json"
    opened_urls: list[str] = []
    monkeypatch.setattr(
        webbrowser,
        "open",
        lambda url, new=0: opened_urls.append(url) or True,
    )
    task = asyncio.create_task(
        serve_application(
            listener,
            port=port,
            auth_token="d" * 32,
            challenge="e" * 32,
            log_level="ERROR",
            on_ready=started.set,
            open_browser=False,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=2,
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            page = await client.get("/")
            assert page.status_code == 200
            assert "本地文献收件箱" in page.text
            assert "script-src 'self'" in page.headers["content-security-policy"]
            app_js = await client.get("/static/js/app.js")
            assert app_js.status_code == 200
            assert "beginSearch" in app_js.text

            session = await client.get(
                "/api/v1/system/health?challenge=" + "e" * 32
            )
            assert session.json()["session_token"] == "d" * 32
            write_runtime(
                runtime_file,
                RuntimeRecord(pid=os.getpid(), port=port, challenge="e" * 32),
            )
            reopened = await asyncio.to_thread(
                _open_existing_instance,
                runtime_file,
                wait_seconds=1,
            )
            assert reopened is True
            assert opened_urls == [f"http://127.0.0.1:{port}/#token={'d' * 32}"]

            denied = await client.get("/api/v1/categories")
            assert denied.status_code == 403

            first_tab = await client.post(
                "/api/v1/system/heartbeat",
                headers={
                    "X-App-Token": "d" * 32,
                    "Origin": f"http://127.0.0.1:{port}",
                },
                json={"client_id": "1" * 32, "active": True},
            )
            second_tab = await client.post(
                "/api/v1/system/heartbeat",
                headers={
                    "X-App-Token": "d" * 32,
                    "Origin": f"http://127.0.0.1:{port}",
                },
                json={"client_id": "2" * 32, "active": True},
            )
            assert first_tab.status_code == second_tab.status_code == 200

            close_one = await client.post(
                "/api/v1/system/heartbeat",
                headers={
                    "X-App-Token": "d" * 32,
                    "Origin": f"http://127.0.0.1:{port}",
                },
                json={"client_id": "1" * 32, "active": False},
            )
            assert close_one.status_code == 200
            still_running = await client.get("/health")
            assert still_running.status_code == 200

            close_last = await client.post(
                "/api/v1/system/heartbeat",
                headers={
                    "X-App-Token": "d" * 32,
                    "Origin": f"http://127.0.0.1:{port}",
                },
                json={"client_id": "2" * 32, "active": False},
            )
            assert close_last.status_code == 200
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        listener.close()
