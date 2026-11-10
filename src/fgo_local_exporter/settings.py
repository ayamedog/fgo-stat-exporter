"""桌面接口与资源路径；仅允许本机回环地址。"""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fgo_local_exporter.errors import ExportError

__all__ = ["LocalConfig"]


@dataclass(frozen=True, repr=False)
class LocalConfig:
    origin: str = "http://127.0.0.1:55313"
    engine_source: Path | None = None
    data_dir: Path | None = None
    atlas_dir: Path | None = None
    atlas_jp_dir: Path | None = None
    asset_dir: Path | None = None
    native_ui_dir: Path | None = None
    header_font: Path | None = None
    regular_font: Path | None = None
    max_sessions: int = 4
    memory_bytes: int = 192 * 1024 * 1024

    def __post_init__(self):
        try:
            parsed = urlsplit(self.origin)
            host, port = parsed.hostname, parsed.port
        except (TypeError, ValueError):
            raise ExportError("LOCAL_CONFIG_INVALID") from None
        authority = f"[{host}]" if host == "::1" else host
        canonical = f"http://{authority}" + (f":{port}" if port not in (None, 80) else "")
        if (
            parsed.scheme != "http"
            or host not in {"127.0.0.1", "localhost", "::1"}
            or port == 0
            or self.origin != canonical
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not 1 <= self.max_sessions <= 4
            or not 1024 * 1024 <= self.memory_bytes <= 256 * 1024 * 1024
        ):
            raise ExportError("LOCAL_CONFIG_INVALID")

    @property
    def host(self):
        return urlsplit(self.origin).netloc
