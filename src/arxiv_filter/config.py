"""Atomic local preferences with secret-safe API representations."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

@dataclass(frozen=True, slots=True)
class AppSettings:
    proxy_mode: str = "system"
    proxy_url: str | None = None
    open_marks_read: bool = False

    @property
    def trust_env(self) -> bool:
        return self.proxy_mode == "system"

    @property
    def effective_proxy(self) -> str | None:
        return self.proxy_url if self.proxy_mode == "custom" else None

    def public(self) -> dict[str, object]:
        display = ""
        if self.proxy_url:
            parsed = urlsplit(self.proxy_url)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = f":{parsed.port}" if parsed.port else ""
            credentials = "***@" if parsed.username is not None or parsed.password is not None else ""
            display = f"{parsed.scheme}://{credentials}{host}{port}"
        return {
            "proxy_mode": self.proxy_mode,
            "proxy_configured": bool(self.proxy_url),
            "proxy_display": display,
            "open_marks_read": self.open_marks_read,
        }


class SettingsStore:
    """Read and atomically persist a small config.json document."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._settings = self._load()

    @property
    def settings(self) -> AppSettings:
        return self._settings

    def update(
        self,
        *,
        proxy_mode: str,
        proxy_url: str | None,
        open_marks_read: bool,
    ) -> AppSettings:
        if proxy_mode not in {"system", "none", "custom"}:
            raise ValueError("不支持的代理模式")
        candidate_url = self._settings.proxy_url
        if proxy_url:
            candidate_url = validate_proxy_url(proxy_url)
        if proxy_mode == "custom" and not candidate_url:
            raise ValueError("自定义代理模式需要提供代理地址")
        settings = AppSettings(proxy_mode, candidate_url, bool(open_marks_read))
        self._save(settings)
        self._settings = settings
        return settings

    def _load(self) -> AppSettings:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return AppSettings()
            proxy_mode = payload.get("proxy_mode", "system")
            proxy_url = payload.get("proxy_url")
            open_marks_read = payload.get("open_marks_read", False)
            if proxy_mode not in {"system", "none", "custom"}:
                return AppSettings()
            if proxy_url is not None:
                proxy_url = validate_proxy_url(proxy_url)
            return AppSettings(proxy_mode, proxy_url, bool(open_marks_read))
        except FileNotFoundError:
            return AppSettings()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Keep a corrupt document intact for diagnosis; the application can
            # run with defaults and only replaces it after an explicit save.
            return AppSettings()

    def _save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "proxy_mode": settings.proxy_mode,
            "proxy_url": settings.proxy_url,
            "open_marks_read": settings.open_marks_read,
        }
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def validate_proxy_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)
        or any(char.isspace() for char in value)
    ):
        raise ValueError("代理地址格式无效")
    text = value.strip()
    try:
        parsed = urlsplit(text)
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError as exc:
        raise ValueError("代理地址格式无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not valid_port:
        raise ValueError("代理地址须为有效的 HTTP 或 HTTPS URL")
    if parsed.fragment:
        raise ValueError("代理地址不能包含片段")
    return text


__all__ = ["AppSettings", "SettingsStore", "validate_proxy_url"]
