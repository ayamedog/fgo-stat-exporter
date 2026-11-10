"""旧版 DPAPI 配置的迁移兼容；正常启动不再读取或生成该缓存。"""

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.game_profile import GameProfile, checked_profile

__all__ = ["load_cached_profile", "save_cached_profile"]

_MAGIC = b"FGOP1\0"
_ENTROPY = b"FGOStatExporter.protocol-config.v1"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(raw, decrypt=False):
    if os.name != "nt":
        raise ExportError("LOCAL_PROFILE_ENCRYPTION_UNAVAILABLE")
    data = ctypes.create_string_buffer(raw)
    source = _Blob(len(raw), ctypes.cast(data, ctypes.POINTER(ctypes.c_ubyte)))
    salt = ctypes.create_string_buffer(_ENTROPY)
    entropy = _Blob(len(_ENTROPY), ctypes.cast(salt, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    function = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    function.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    function.restype = wintypes.BOOL
    if not function(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)
    ):
        raise ExportError("LOCAL_PROFILE_ENCRYPTION_FAILED")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def _checked(value):
    return checked_profile(value)


def save_cached_profile(path: Path, profile: GameProfile):
    value = {
        key: getattr(profile, key)
        for key in ("version", "sdk_base", "sdk_key", "development_code", "session_prefix")
    }
    _checked(value)
    raw = _MAGIC + _crypt(json.dumps(value, separators=(",", ":")).encode())
    temp = path.with_suffix(".partial")
    if path.is_symlink() or temp.is_symlink():
        raise ExportError("LOCAL_PROFILE_INVALID")
    with temp.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def load_cached_profile(path: Path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or not 10 < path.stat().st_size < 16384:
        raise ExportError("LOCAL_PROFILE_INVALID")
    raw = path.read_bytes()
    if not raw.startswith(_MAGIC):
        raise ExportError("LOCAL_PROFILE_INVALID")
    try:
        return _checked(json.loads(_crypt(raw[len(_MAGIC) :], decrypt=True)))
    except (ValueError, TypeError):
        raise ExportError("LOCAL_PROFILE_INVALID") from None
