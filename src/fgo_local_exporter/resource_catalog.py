"""读取公开资源版本；只接收字典，不下载或执行程序更新。"""

import hashlib
import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from fgo_local_exporter.errors import ExportError

__all__ = ["PublicResourceClient", "ResourceCatalog", "discover_catalog", "validate_url"]

_DATA = "https://raw.githubusercontent.com/chaldea-center/chaldea-data/main/dist/"
_INFO = "https://api.atlasacademy.io/export/CN/info.json"
_EXTRA = "https://data-cn.chaldea.center/mstGachaCNExtra.json"
_ATLAS = "https://api.atlasacademy.io/repo/CN/master/"
_JP_INFO = "https://api.atlasacademy.io/export/JP/info.json"
_JP_ATLAS = "https://api.atlasacademy.io/repo/JP/master/"
_SINGLES = frozenset(
    {
        "items.json",
        "entities.json",
        "mappingPatch.json",
        "classBoards.json",
        "grandGraphs.json",
        "constData.json",
        "baseSkills.json",
    }
)
_PARTS = ("servants", "craftEssences", "wars", "wiki.servants", "mappingData")
_FILE_LIMIT = 16 * 1024 * 1024


def validate_url(url):
    """地址由客户端固定代码拼接；拒绝其他仓库、查询参数和跳转。"""
    if not isinstance(url, str):
        raise ExportError("RESOURCE_MANIFEST_INVALID")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment or ".." in parsed.path:
        raise ExportError("RESOURCE_MANIFEST_INVALID")
    allowed = (
        parsed.netloc == "raw.githubusercontent.com"
        and re.fullmatch(
            r"/chaldea-center/chaldea-data/(?:main|[0-9a-f]{40})/dist/[A-Za-z0-9_.-]+\.json",
            parsed.path,
        )
    ) or url in {
        _INFO,
        _EXTRA,
        _ATLAS + "mstGacha.json",
        _ATLAS + "mstShop.json",
        _JP_INFO,
        _JP_ATLAS + "mstGacha.json",
        _JP_ATLAS + "mstShop.json",
    }
    if not allowed:
        raise ExportError("RESOURCE_MANIFEST_INVALID")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ExportError("RESOURCE_REDIRECT_REJECTED")


class PublicResourceClient:
    def get(self, url, limit, *, progress=None, stop=None):
        validate_url(url)
        if not 0 < limit <= _FILE_LIMIT:
            raise ExportError("RESOURCE_MANIFEST_INVALID")
        client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        try:
            with client.open(
                urllib.request.Request(
                    url, headers={"User-Agent": "FGOStatExporter/0.4", "Cache-Control": "no-cache"}
                ),
                timeout=30,
            ) as response:
                chunks, count = [], 0
                while count <= limit:
                    if stop is not None and stop.is_set():
                        raise ExportError("CANCELLED")
                    chunk = response.read(min(64 * 1024, limit + 1 - count))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    count += len(chunk)
                    if progress:
                        progress(count)
                if count > limit:
                    raise ExportError("RESOURCE_SIZE_LIMIT")
                return b"".join(chunks)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ExportError("RESOURCE_DOWNLOAD_FAILED") from None


def _json(raw):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ExportError("RESOURCE_MANIFEST_INVALID") from None


def _selected_files(version):
    if (
        not isinstance(version, dict)
        or type(version.get("timestamp")) is not int
        or not 0 < version["timestamp"] < 32_503_680_000
    ):
        raise ExportError("RESOURCE_MANIFEST_INVALID")
    files = version.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 1024:
        raise ExportError("RESOURCE_MANIFEST_INVALID")
    names = set(_SINGLES)
    for prefix in _PARTS:
        matching = {
            name for name in files if re.fullmatch(re.escape(prefix) + r"\.[1-9][0-9]*\.json", name)
        }
        if not matching or len(matching) > 64:
            raise ExportError("RESOURCE_CATALOG_INCOMPLETE")
        if matching != {f"{prefix}.{i}.json" for i in range(1, len(matching) + 1)}:
            raise ExportError("RESOURCE_CATALOG_INCOMPLETE")
        names.update(matching)
    if not names.issubset(files) or len(names) > 192:
        raise ExportError("RESOURCE_CATALOG_INCOMPLETE")
    specs = []
    for name in sorted(names):
        row = files[name]
        if (
            not isinstance(row, dict)
            or row.get("filename") != name
            or type(row.get("size")) is not int
            or not 0 < row["size"] <= _FILE_LIMIT
            or not isinstance(row.get("hash"), str)
            or not re.fullmatch(r"[0-9a-f]{6,32}", row["hash"])
        ):
            raise ExportError("RESOURCE_MANIFEST_INVALID")
        specs.append(
            {
                "filename": name,
                "url": _DATA + name,
                "bytes": row["size"],
                "upstream_md5": row["hash"],
            }
        )
    if sum(spec["bytes"] for spec in specs) > 192 * 1024 * 1024:
        raise ExportError("RESOURCE_SIZE_LIMIT")
    return specs


def _atlas_info(raw):
    value = _json(raw)
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("hash"), str)
        or not re.fullmatch(r"[0-9a-f]{6,40}", value["hash"])
        or type(value.get("timestamp")) is not int
        or type(value.get("dataVer")) is not int
        or not 0 < value["timestamp"] < 32_503_680_000
        or not 0 < value["dataVer"] < 1_000_000
    ):
        raise ExportError("RESOURCE_MANIFEST_INVALID")
    return value


@dataclass
class ResourceCatalog:
    manifest: dict
    supplied: dict
    version_raw: bytes
    atlas_info: dict
    extra_digest: str
    atlas_jp_info: dict | None = None

    def verify_current(self, client, stop):
        """下载前后比较源站版本，避免把更新过程中的两批文件混用。"""
        version = client.get(_DATA + "version.json", 256 * 1024, stop=stop)
        info = _atlas_info(client.get(_INFO, 64 * 1024, stop=stop))
        extra = client.get(_EXTRA, 2 * 1024 * 1024, stop=stop)
        jp_info = _atlas_info(client.get(_JP_INFO, 64 * 1024, stop=stop))
        if (
            version != self.version_raw
            or info != self.atlas_info
            or hashlib.sha256(extra).hexdigest() != self.extra_digest
            or jp_info != self.atlas_jp_info
        ):
            raise ExportError("RESOURCE_VERSION_CHANGED")


def discover_catalog(client, stop):
    raw = client.get(_DATA + "version.json", 256 * 1024, stop=stop)
    version = _json(raw)
    specs = _selected_files(version)
    info = _atlas_info(client.get(_INFO, 64 * 1024, stop=stop))
    jp_info = _atlas_info(client.get(_JP_INFO, 64 * 1024, stop=stop))
    extra = client.get(_EXTRA, 2 * 1024 * 1024, stop=stop)
    if not isinstance(_json(extra), list):
        raise ExportError("RESOURCE_MANIFEST_INVALID")
    revision, extra_digest = hashlib.sha256(raw).hexdigest(), hashlib.sha256(extra).hexdigest()
    label = (
        f"cn-jp-{version['timestamp']}-{revision[:10]}-"
        f"{info['hash']}-{jp_info['hash']}-{extra_digest[:8]}"
    )
    manifest = {
        "version": label,
        "groups": {
            "data": {
                "data_revision": revision,
                "revision_kind": "version-json-sha256",
                "resource_version": label,
                "files": [
                    *specs,
                    {
                        "filename": "version.json",
                        "url": _DATA + "version.json",
                        "bytes": len(raw),
                        "sha256": revision,
                    },
                ],
            },
            "atlas-cn": {
                "region": "CN",
                "atlas_info": info,
                "resource_version": label,
                "files": [
                    {"filename": name, "url": _ATLAS + name}
                    for name in ("mstGacha.json", "mstShop.json")
                ]
                + [
                    {
                        "filename": "mstGachaCNExtra.json",
                        "url": _EXTRA,
                        "bytes": len(extra),
                        "sha256": extra_digest,
                    }
                ],
            },
            "atlas-jp": {
                "region": "JP",
                "atlas_info": jp_info,
                "resource_version": label,
                "files": [
                    {"filename": name, "url": _JP_ATLAS + name}
                    for name in ("mstGacha.json", "mstShop.json")
                ],
            },
        },
    }
    return ResourceCatalog(
        manifest,
        {("data", "version.json"): raw, ("atlas-cn", "mstGachaCNExtra.json"): extra},
        raw,
        info,
        extra_digest,
        jp_info,
    )
