"""Runtime dependency assembly and graceful uvicorn lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import uvicorn

from arxiv_filter.api import ApiServices, create_api_app
from arxiv_filter.arxiv.client import ArxivClient, ClientConfig
from arxiv_filter.arxiv.curl_transport import active_transport_choice, build_transport
from arxiv_filter.config import SettingsStore
from arxiv_filter.exporters import CompositeExporter
from arxiv_filter.services.job_manager import JobManager
from arxiv_filter.services.search_service import SearchService
from arxiv_filter.storage.cache_repo import CacheRepository
from arxiv_filter.storage.database import Database
from arxiv_filter.storage.history_repo import HistoryRepository
from arxiv_filter.storage.read_repo import ReadRepository
from arxiv_filter.taxonomy import load_taxonomy

logger = logging.getLogger(__name__)
IDLE_EXIT_SECONDS = 30 * 60
HEARTBEAT_POLL_SECONDS = 1.0
BROWSER_CLOSE_GRACE_SECONDS = 15.0


@dataclass(slots=True)
class RuntimeResources:
    database: Database
    provider: ArxivClient
    jobs: JobManager
    services: ApiServices

    async def close(self) -> None:
        try:
            active = await self.jobs.active_job()
            if active is not None:
                await self.jobs.cancel(active.job_id)
                await self.jobs.wait(active.job_id)
        finally:
            try:
                await self.provider.aclose()
            finally:
                await self.database.close()


async def build_runtime_resources(
    *,
    port: int,
    auth_token: str,
    challenge: str,
    shutdown_signal: asyncio.Event,
) -> RuntimeResources:
    """Open the local database and wire API services without making network calls."""
    from arxiv_filter.paths import backup_dir, config_path, db_path

    taxonomy = load_taxonomy()
    database = Database(db_path(), backup_dir=backup_dir())
    await database.open()
    cache = CacheRepository(database)
    history = HistoryRepository(database)
    reads = ReadRepository(database)
    settings = SettingsStore(config_path())
    # 传输层：默认 curl，绕开 arXiv 边缘层对 httpx 指纹的间歇性 406（见
    # arxiv/curl_transport.py 与 docs/api-verification.md §3）。curl 不可用时
    # 自动回退 httpx 并记 WARNING；设 ARXIV_FILTER_TRANSPORT=httpx 可一键退回。
    transport = build_transport()
    logger.info("arXiv 传输层：%s", active_transport_choice())
    provider = ArxivClient(
        taxonomy=taxonomy,
        config=ClientConfig(
            proxy_url=settings.settings.effective_proxy,
            trust_env=settings.settings.trust_env,
            transport=transport,
        ),
    )
    search = SearchService(cache, history, provider, taxonomy=taxonomy)
    jobs = JobManager(search, read_repository=reads)
    services = ApiServices(
        taxonomy=taxonomy,
        jobs=jobs,
        read_repository=reads,
        history_repository=history,
        cache_repository=cache,
        auth_token=auth_token,
        schema_version=database.schema_version,
        allowed_origin=f"http://127.0.0.1:{port}",
        launcher_challenge=challenge,
        shutdown_signal=shutdown_signal,
        settings_store=settings,
        provider=provider,
        exporter=CompositeExporter(),
    )
    if database.migration_error is not None:
        logger.error("数据库迁移失败，已以只读模式启动：%s", database.migration_error.message_zh)
    elif database.read_only:
        logger.warning("数据库版本不兼容，已以只读模式启动")
    return RuntimeResources(database, provider, jobs, services)


async def serve_application(
    listener,
    *,
    port: int,
    auth_token: str,
    challenge: str,
    log_level: str,
    on_ready: Callable[[], None],
    open_browser: bool,
) -> None:
    """Run the app on a pre-bound loopback socket and stop cleanly on idle/shutdown."""
    shutdown_signal = asyncio.Event()
    resources = await build_runtime_resources(
        port=port,
        auth_token=auth_token,
        challenge=challenge,
        shutdown_signal=shutdown_signal,
    )
    app = create_api_app(resources.services)
    app.state.runtime_resources = resources

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_config=None,
        log_level=log_level.lower(),
        access_log=False,
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=10,
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not server.started:
            if serve_task.done():
                await serve_task
                raise RuntimeError("本地 Web 服务未能启动")
            if asyncio.get_running_loop().time() >= deadline:
                server.should_exit = True
                raise TimeoutError("本地 Web 服务启动超时")
            await asyncio.sleep(0.02)

        on_ready()
        if open_browser:
            from urllib.parse import quote
            import webbrowser

            url = f"http://127.0.0.1:{port}/#token={quote(auth_token, safe='')}"
            opened = await asyncio.to_thread(webbrowser.open, url, new=2)
            if not opened:
                logger.warning("默认浏览器未能自动打开；可重新双击程序打开现有会话")

        await _monitor_server(server, serve_task, resources, shutdown_signal)
        await serve_task
    finally:
        if not serve_task.done():
            server.should_exit = True
            try:
                await asyncio.wait_for(serve_task, timeout=12)
            except TimeoutError:
                server.force_exit = True
                await serve_task
        await resources.close()


async def _monitor_server(
    server: uvicorn.Server,
    serve_task: asyncio.Task[None],
    resources: RuntimeResources,
    shutdown_signal: asyncio.Event,
) -> None:
    while not serve_task.done():
        if shutdown_signal.is_set():
            server.should_exit = True
            return
        now = time.monotonic()
        resources.services.prune_browser_presence(now=now)
        if resources.services.browser_client_seen:
            no_browser_since = resources.services.no_browser_since_monotonic
            if (
                no_browser_since is not None
                and now - no_browser_since >= BROWSER_CLOSE_GRACE_SECONDS
            ):
                if await resources.jobs.active_job() is None:
                    logger.info("最后一个浏览器页面已关闭且没有活动任务，正在退出")
                    server.should_exit = True
                    return
        elif now - resources.services.last_heartbeat_monotonic >= IDLE_EXIT_SECONDS:
            if await resources.jobs.active_job() is None:
                logger.info("没有浏览器页面且没有活动任务，空闲退出")
                server.should_exit = True
                return
        await asyncio.sleep(HEARTBEAT_POLL_SECONDS)


__all__ = [
    "BROWSER_CLOSE_GRACE_SECONDS",
    "HEARTBEAT_POLL_SECONDS",
    "IDLE_EXIT_SECONDS",
    "RuntimeResources",
    "build_runtime_resources",
    "serve_application",
]
