"""Offline candidate: transform only externally approved .imy members in memory.

This development tool does not approve a release and is not a Gradle hook.
"""

import hashlib
import importlib.util
import io
import json
import marshal
import re
import types
import zipfile

MAX_MEMBER = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
AUTHORIZED_PAGES = frozenset(
    {
        "fgo_local_exporter/desktop_static/desktop.css",
        "fgo_local_exporter/desktop_static/setup.html",
        "fgo_local_exporter/mobile_static/mobile.css",
        "fgo_local_exporter/mobile_static/mobile.js",
        "fgo_local_exporter/ui/app.js",
        "fgo_local_exporter/ui/captcha-frame.html",
        "fgo_local_exporter/ui/captcha-frame.js",
        "fgo_local_exporter/ui/gacha-records.css",
        "fgo_local_exporter/ui/gacha-records.js",
        "fgo_local_exporter/ui/html-to-image.js",
        "fgo_local_exporter/ui/index.html",
        "fgo_local_exporter/ui/resource-status.js",
        "fgo_local_exporter/ui/style.css",
        "fgo_stat_core/infrastructure/report_ui/template.html",
    }
)


def strip_code(code):
    if not isinstance(code, types.CodeType):
        raise ValueError("INVALID_CODE")

    def transform(value):
        if isinstance(value, types.CodeType):
            return strip_code(value)
        if isinstance(value, tuple):
            return tuple(transform(item) for item in value)
        if isinstance(value, frozenset):
            return frozenset(transform(item) for item in value)
        return value

    return code.replace(
        co_filename="<release>",
        co_firstlineno=1,
        co_linetable=b"",
        co_consts=tuple(transform(value) for value in code.co_consts),
    )


def strip_pyc(raw):
    if len(raw) < 16 or raw[:4] != importlib.util.MAGIC_NUMBER:
        raise ValueError("INVALID_PYC")
    source = io.BytesIO(raw[16:])
    try:
        code = marshal.load(source)
        if source.read(1):
            raise ValueError("PYC_TRAILING_DATA")
        transformed = strip_code(code)
    except (TypeError, EOFError) as exc:
        raise ValueError("INVALID_PYC") from exc
    return importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(transformed)


def rewrite_imy(raw, approved, *, allow_frontend=False):
    """Return transformed archive, requiring exact *independently approved* inputs.

    approved: mapping member -> {sha256, action}; action is 'strip_pyc' or 'copy'.
    The caller must independently authorize and later freeze the output inventory.
    """
    if not isinstance(approved, dict) or len(approved) > 10000:
        raise ValueError("APPROVAL_INVALID")
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        members = source.infolist()
        names = [item.filename for item in members]
        if len(names) != len(set(names)) or set(names) != set(approved):
            raise ValueError("MEMBER_SET_MISMATCH")
        total = 0
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for item in sorted(members, key=lambda x: x.filename):
                name = item.filename
                components = name[:-1].split("/") if item.is_dir() else name.split("/")
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or any(x in ("", ".", "..") for x in components)
                    or item.flag_bits & 1
                    or item.file_size > MAX_MEMBER
                    or ((item.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise ValueError("MEMBER_INVALID")
                total += item.file_size
                if total > MAX_TOTAL:
                    raise ValueError("ARCHIVE_TOO_LARGE")
                spec = approved[name]
                if not isinstance(spec, dict) or set(spec) not in (
                    {"sha256", "action"},
                    {"normalized_sha256", "action"},
                ):
                    raise ValueError("APPROVAL_INVALID")
                contents = source.read(item)
                if "normalized_sha256" in spec:
                    if spec["action"] not in {"strip_pyc", "drop"} or not name.endswith(".pyc"):
                        raise ValueError("ACTION_INVALID")
                    contents = strip_pyc(contents)
                    if hashlib.sha256(contents).hexdigest() != spec["normalized_sha256"]:
                        raise ValueError("MEMBER_HASH_MISMATCH")
                elif hashlib.sha256(contents).hexdigest() != spec["sha256"]:
                    raise ValueError("MEMBER_HASH_MISMATCH")
                if spec["action"] == "drop":
                    continue
                if name.startswith("werkzeug/debug/shared/"):
                    raise ValueError("DEBUG_RESOURCE_FORBIDDEN")
                if re.search(r"\.(?:js|html|css)$", name, re.I) and (
                    not allow_frontend or name not in AUTHORIZED_PAGES
                ):
                    raise ValueError("UNAUTHORIZED_FRONTEND")
                if name in {"anyio/pytest_plugin.pyc", "flask/testing.pyc", "click/testing.pyc"}:
                    raise ValueError("TEST_SUPPORT_MODULE_FORBIDDEN")
                if name == "doctest.pyc" or name.startswith(("unittest/", "_pyrepl/")):
                    raise ValueError("TEST_OR_REPL_MODULE_FORBIDDEN")
                if re.match(
                    r"^(?:xxlimited(?:_35)?|xxsubtype|_xxtestfuzz)(?:\.|$)", name.rsplit("/", 1)[-1]
                ):
                    raise ValueError("CPYTHON_TEST_EXTENSION_FORBIDDEN")
                if re.search(r"\.(?:py|pyi|pyo|map|c|cc|cpp|h|hpp)$", name, re.I):
                    raise ValueError("SOURCE_OR_DEBUG_FORBIDDEN")
                if item.is_dir():
                    if spec["action"] != "copy" or contents:
                        raise ValueError("ACTION_INVALID")
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    info.external_attr = (0o40755 << 16) | 0x10
                    target.writestr(info, b"")
                    continue
                if spec["action"] == "strip_pyc" and name.endswith(".pyc") and "sha256" in spec:
                    contents = strip_pyc(contents)
                elif not (spec["action"] == "strip_pyc" and name.endswith(".pyc")) and (
                    spec["action"] != "copy" or name.endswith(".pyc")
                ):
                    raise ValueError("ACTION_INVALID")
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                target.writestr(info, contents)
    return output.getvalue()


def rewrite_build_json(raw, original_assets, replacements):
    """Update Chaquopy's SHA-1 inventory after separately authorized rewrites.

    The caller supplies every original build asset, plus an explicit subset of
    replacements. This function is not an approval of those bytes.
    """

    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("DUPLICATE_JSON_KEY")
            obj[key] = value
        return obj

    try:
        data = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("BUILD_JSON_INVALID") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"assets", "extract_packages", "python_version"}
        or not isinstance(data["assets"], dict)
        or not isinstance(data["extract_packages"], list)
        or not isinstance(data["python_version"], str)
        or set(data["assets"]) != set(original_assets)
        or not set(replacements).issubset(original_assets)
    ):
        raise ValueError("BUILD_ASSET_SET_MISMATCH")
    for name, content in original_assets.items():
        if (
            not isinstance(content, bytes)
            or data["assets"][name] != hashlib.sha1(content).hexdigest()
        ):
            raise ValueError("BUILD_ASSET_HASH_MISMATCH")
    for name, content in replacements.items():
        if not isinstance(content, bytes):
            raise ValueError("BUILD_ASSET_INVALID")
        data["assets"][name] = hashlib.sha1(content).hexdigest()
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
