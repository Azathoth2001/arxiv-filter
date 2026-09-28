"""注入 API route 所需的应用依赖。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from arxiv_filter import SCHEMA_VERSION, __version__
from arxiv_filter.api.ports import EmptyLogReader, Exporter, LogReader
from arxiv_filter.config import SettingsStore
from arxiv_filter.services.job_manager import JobManager
from arxiv_filter.storage.cache_repo import CacheRepository
from arxiv_filter.storage.database import utc_now
from arxiv_filter.storage.history_repo import HistoryRepository
from arxiv_filter.storage.read_repo import ReadRepository
from arxiv_filter.taxonomy import Taxonomy

# A closed-page notification normally stops the server in seconds. Keep a
# generous lease as fallback so sleeping/throttled background tabs are not
# mistaken for closed windows.
BROWSER_HEARTBEAT_TTL_SECONDS = 30 * 60.0


@dataclass(slots=True)
class ApiServices:
    taxonomy: Taxonomy
    jobs: JobManager
    read_repository: ReadRepository
    history_repository: HistoryRepository
    cache_repository: CacheRepository
    auth_token: str
    app_version: str = __version__
    schema_version: int = SCHEMA_VERSION
    clock: Callable[[], datetime] = utc_now
    log_reader: LogReader = field(default_factory=EmptyLogReader)
    exporter: Exporter | None = None
    settings_store: SettingsStore | None = None
    provider: object | None = None
    allowed_origin: str | None = None
    launcher_challenge: str | None = None
    shutdown_signal: asyncio.Event = field(default_factory=asyncio.Event)
    last_heartbeat_monotonic: float = field(default_factory=time.monotonic)
    active_browser_clients: dict[str, float] = field(default_factory=dict)
    browser_client_seen: bool = False
    no_browser_since_monotonic: float | None = None

    def __post_init__(self) -> None:
        if len(self.auth_token) < 32:
            raise ValueError("API token 必须至少为 128-bit（32 个十六进制字符）")
        self.jobs.bind_read_repository(self.read_repository)

    def update_browser_presence(
        self, client_id: str, *, active: bool, now: float | None = None
    ) -> None:
        """Record one browser tab heartbeat or its best-effort close notification."""
        seen_at = time.monotonic() if now is None else now
        self.last_heartbeat_monotonic = seen_at
        self.browser_client_seen = True
        self.active_browser_clients = {
            key: timestamp
            for key, timestamp in self.active_browser_clients.items()
            if seen_at - timestamp <= BROWSER_HEARTBEAT_TTL_SECONDS
        }
        if active:
            self.active_browser_clients[client_id] = seen_at
            self.no_browser_since_monotonic = None
            return

        self.active_browser_clients.pop(client_id, None)
        if not self.active_browser_clients and self.no_browser_since_monotonic is None:
            self.no_browser_since_monotonic = seen_at

    def prune_browser_presence(self, *, now: float | None = None) -> None:
        """Expire missed heartbeats, tolerating background-tab timer throttling."""
        seen_at = time.monotonic() if now is None else now
        self.active_browser_clients = {
            key: timestamp
            for key, timestamp in self.active_browser_clients.items()
            if seen_at - timestamp <= BROWSER_HEARTBEAT_TTL_SECONDS
        }
        if self.active_browser_clients:
            self.no_browser_since_monotonic = None
        elif self.browser_client_seen and self.no_browser_since_monotonic is None:
            self.no_browser_since_monotonic = seen_at


__all__ = ["ApiServices", "BROWSER_HEARTBEAT_TTL_SECONDS"]
