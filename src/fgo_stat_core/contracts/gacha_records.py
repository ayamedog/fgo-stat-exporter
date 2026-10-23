"""卡池累计抽数的展示契约，不携带账号或实例标识。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["GachaHistory", "GachaRecord"]

type Count = Annotated[int, Field(ge=0)]
type EntityId = Annotated[int, Field(gt=0)]
type Timestamp = Annotated[int, Field(gt=0, le=253402300799)]
type Name = Annotated[str, Field(max_length=160)]


class _DetailModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    def __str__(self) -> str:
        return repr(self)


class GachaRecord(_DetailModel):
    gacha_id: EntityId
    name: Name | None
    pulls: Count
    kind: Literal["stone", "friend", "lucky", "ticket", "unknown"]
    opened_at: Timestamp | None
    closed_at: Timestamp | None
    image_id: EntityId | None


class GachaHistory(_DetailModel):
    region: Literal["CN", "JP"] = "CN"
    timezone: Literal["Asia/Shanghai", "Asia/Tokyo"] = "Asia/Shanghai"
    captured_at: Timestamp
    available: bool
    records: tuple[GachaRecord, ...]

    @property
    def byte_size(self) -> int:
        return len(self.model_dump_json().encode())
