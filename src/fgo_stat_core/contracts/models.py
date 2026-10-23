"""统计数据契约；敏感输入不参与对象 repr。"""

import math
from dataclasses import dataclass, field
from typing import Literal, Protocol

__all__ = ["CaptureError", "Limits", "Parser", "Snapshot", "TableRows", "Value"]

type Value = int | str | tuple[int, ...] | tuple[str, ...]
type TableRows = tuple[dict[str, Value], ...]


class CaptureError(Exception):
    """只携带固定错误码，禁止加入原始异常或输入值。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Limits:
    input_bytes: int = 16 * 1024 * 1024
    decoded_bytes: int = 32 * 1024 * 1024
    max_depth: int = 32
    max_nodes: int = 1_000_000
    max_rows: int = 100_000
    max_layers: int = 4
    parse_seconds: float = 8.0
    worker_memory_mb: int = 512
    download_seconds: float = 30.0
    concurrency: int = 2
    preview_seconds: int = 600
    max_pending: int = 64
    event_seconds: int = 86400
    max_events: int = 4096

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            expected = (
                (int, float)
                if name.endswith("_seconds") and name in {"parse_seconds", "download_seconds"}
                else (int,)
            )
            if type(value) not in expected or not math.isfinite(value) or value <= 0:
                raise CaptureError("CONFIG_INVALID")


@dataclass(frozen=True, repr=False)
class Snapshot:
    schema_version: str
    region: Literal["CN", "JP"]
    account_key: str
    captured_at: int
    tables: dict[str, TableRows]
    warnings: tuple[str, ...] = ()
    table_times: dict[str, int] = field(default_factory=dict)


class Parser(Protocol):
    async def parse(self, raw: bytes) -> Snapshot: ...
