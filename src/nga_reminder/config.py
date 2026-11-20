"""Runtime configuration.

Credentials may come from two places, in this order of precedence:

1. the local SQLite store (written by the web UI's setup form),
2. environment variables / a ``.env`` file.

Nothing here ever logs a credential.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    """Process-wide settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- NGA credentials -------------------------------------------------
    nga_uid: str = Field(default="", alias="NGA_UID")
    nga_cid: str = Field(default="", alias="NGA_CID")

    # --- NGA endpoint ----------------------------------------------------
    nga_base_url: str = Field(default="https://bbs.nga.cn", alias="NGA_BASE_URL")
    nga_ua: str = Field(default=DEFAULT_UA, alias="NGA_UA")
    request_timeout: float = Field(default=30.0, alias="NGA_TIMEOUT")

    # --- polling ---------------------------------------------------------
    poll_interval: int = Field(default=60, alias="POLL_INTERVAL")
    delay_ms: int = Field(default=330, alias="NGA_DELAY_MS")
    max_retries: int = Field(default=3, alias="NGA_MAX_RETRIES")

    # --- local storage / server -----------------------------------------
    db_path: Path = Field(default=Path("data/nga.db"), alias="NGA_DB_PATH")
    host: str = Field(default="127.0.0.1", alias="NGA_HOST")
    port: int = Field(default=8765, alias="NGA_PORT")
    open_browser: bool = Field(default=True, alias="NGA_OPEN_BROWSER")

    #: 点击桌面通知时打开的地址——指向本项目自己的界面。
    app_url: str = Field(default="http://127.0.0.1:8765/", alias="NGA_APP_URL")

    # --- notifications ---------------------------------------------------
    # "NGA Reminder" is an AppUserModelID Windows accepts without prior
    # registration -- it creates the identity on first use, so a toast sent
    # under it still reaches the notification centre.  No registration step
    # (and therefore no autostart setup) is needed.
    toast_app_id: str = Field(default="NGA Reminder", alias="NGA_TOAST_APP_ID")

    #: HTTP 通知桥接地址。容器里弹不了桌面通知，
    #: 把它指向宿主机上跑的 scripts/notify_bridge.py 就能补上桌面弹窗。
    notify_webhook: str = Field(default="", alias="NGA_NOTIFY_WEBHOOK")
    #: 桥接的共享口令，两端要对上（桥接端读 NGA_BRIDGE_TOKEN）。
    notify_webhook_token: str = Field(default="", alias="NGA_NOTIFY_WEBHOOK_TOKEN")

    def cookie_header(self) -> str:
        return f"ngaPassportUid={self.nga_uid}; ngaPassportCid={self.nga_cid}"

    def has_credentials(self) -> bool:
        return bool(self.nga_uid and self.nga_cid)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
