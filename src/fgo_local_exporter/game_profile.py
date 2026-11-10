"""已验证的游戏协议配置类型；不包含账号授权或实际密钥值。"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fgo_local_exporter.errors import ExportError

__all__ = [
    "GameProfile",
    "checked_profile",
    "default_profile_path",
    "load_desktop_profile",
    "load_profile",
    "profile_values",
]


@dataclass(repr=False)
class GameProfile:
    version: str
    sdk_base: dict[str, str]
    sdk_key: str = field(repr=False)
    development_code: str = field(repr=False)
    session_prefix: str = field(repr=False)


def checked_profile(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "sdk_base",
        "sdk_key",
        "development_code",
        "session_prefix",
    }:
        raise ExportError("LOCAL_PROFILE_INVALID")
    if (
        value["version"] != "2.129.0"
        or not isinstance(value["sdk_base"], dict)
        or set(value["sdk_base"]) != {
            "merchant_id", "game_id", "app_id", "server_id", "version", "sdk_type",
        }
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[0-9]{1,16}", item)
            for item in value["sdk_base"].values()
        )
        or any(
            not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9]{16,128}", value[key])
            for key in ("sdk_key", "development_code", "session_prefix")
        )
    ):
        raise ExportError("LOCAL_PROFILE_INVALID")
    return GameProfile(**value)


def default_profile_path():
    """源码及项目内的 EXE 共用根目录配置，独立 EXE 使用同目录配置。"""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[2] / "game-protocol.json"
    directory = Path(sys.executable).resolve().parent
    for parent in (directory, *directory.parents):
        if (parent / "pyproject.toml").is_file() and (
            parent / "src/fgo_local_exporter/game_profile.py"
        ).is_file():
            return parent / "game-protocol.json"
    return directory / "game-protocol.json"


def load_profile(path):
    """仅读取五项应用级固定配置，拒绝额外凭据字段及超限文件。"""
    try:
        path = Path(path)
        if path.is_symlink():
            raise ExportError("LOCAL_PROFILE_INVALID")
        if not path.exists():
            return None
        if not path.is_file() or not 1 <= path.stat().st_size <= 16384:
            raise ExportError("LOCAL_PROFILE_INVALID")
        with path.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ExportError("LOCAL_PROFILE_INVALID")
        return checked_profile(json.loads(raw))
    except (OSError, ValueError, TypeError):
        raise ExportError("LOCAL_PROFILE_INVALID") from None


def load_desktop_profile(path=None):
    """显式配置优先；独立 EXE 默认使用经审核的内置应用参数。"""
    if path is not None:
        profile = load_profile(path)
        if profile is None:
            raise ExportError("LOCAL_PROFILE_REQUIRED")
        return profile
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        profile = load_profile(Path(sys._MEIPASS) / "runtime-config/game-protocol.json")
        if profile is not None:
            return profile
    return load_profile(default_profile_path())


def profile_values(profile):
    value = {
        key: getattr(profile, key)
        for key in ("version", "sdk_base", "sdk_key", "development_code", "session_prefix")
    }
    checked_profile(value)
    return value
