"""Supplemental Windows release audit using an independently reviewed inventory.

The manifest must be prepared and reviewed outside the delivery directory.  This
tool never treats a build-generated TOC as an authorization or a whitelist.
"""

import argparse
import hashlib
import io
import json
import marshal
import re
import sys
import types
import zipfile
from pathlib import Path, PurePosixPath

import pefile
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from release_parts.runtime import CORE_DATA, STATIC_FILES  # noqa: E402
from review_android import normalized, scan_secret  # noqa: E402

from fgo_local_exporter.game_profile import checked_profile, profile_values  # noqa: E402

MAX_TOTAL = 1024 * 1024 * 1024
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")
FORBIDDEN_SUFFIXES = (
    ".py",
    ".pyi",
    ".pyx",
    ".pxd",
    ".map",
    ".pdb",
    ".jks",
    ".keystore",
    ".p12",
    ".pfx",
    ".dpapi",
    ".env",
    ".log",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fail(code):
    raise RuntimeError(code)


def name_ok(name, *, module=False):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        fail("INVALID_PATH")
    parts = PurePosixPath(name).parts
    raw_parts = name.rstrip("/").split("/")
    if name.startswith("/") or any(p in {"", ".", "..", ".git"} for p in raw_parts):
        fail("INVALID_PATH")
    allowed_framework = module and name in {"jinja2/tests", "werkzeug/test"}
    if not allowed_framework and any(
        p.lower() in {"test", "tests", "__tests__", "fixtures", "selftest"} for p in parts
    ):
        fail("TEST_MATERIAL")
    if name.lower().endswith(FORBIDDEN_SUFFIXES):
        fail("DEVELOPMENT_MATERIAL")


def check_pe(raw):
    try:
        pe = pefile.PE(data=raw, fast_load=True)
        if pe.FILE_HEADER.PointerToSymbolTable or pe.FILE_HEADER.NumberOfSymbols:
            fail("PE_SYMBOL_TABLE")
        if any(
            section.Name.rstrip(b"\0").lower().startswith((b".debug", b".zdebug"))
            for section in pe.sections
        ):
            fail("PE_DEBUG_SECTION")
    except (pefile.PEFormatError, IndexError, AttributeError, ValueError):
        fail("INVALID_PE")


def read_code(raw, *, pyc=False):
    if pyc:
        from importlib.util import MAGIC_NUMBER

        if len(raw) < 16 or raw[:4] != MAGIC_NUMBER:
            fail("PYC_HEADER")
        raw = raw[16:]
    try:
        stream = io.BytesIO(raw)
        code = marshal.load(stream)
        if stream.read(1) or not isinstance(code, types.CodeType):
            fail("PYC_INVALID")
        normalized(code, require_stripped=False)
        return code
    except (EOFError, ValueError, TypeError):
        fail("PYC_INVALID")


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("DUPLICATE_KEY")
        result[key] = value
    return result


def inspect(executable, build, secrets):
    archive = CArchiveReader(str(executable))
    exe = executable.read_bytes()
    if archive._end_offset != len(exe) or archive._start_offset <= 0:
        fail("EXECUTABLE_TRAILING_DATA")
    check_pe(exe[: archive._start_offset])
    # The stock reader silently overwrites duplicate CArchive entries.
    import struct

    cookie = struct.unpack_from(
        CArchiveReader._COOKIE_FORMAT, exe, archive._end_offset - CArchiveReader._COOKIE_LENGTH
    )
    toc_start = archive._start_offset + cookie[2]
    toc_end = toc_start + cookie[3]
    if (
        toc_start < archive._start_offset
        or toc_end > archive._end_offset - CArchiveReader._COOKIE_LENGTH
    ):
        fail("ARCHIVE_TOC_INVALID")
    toc_raw = exe[toc_start:toc_end]
    cursor = 0
    count = 0
    while cursor < len(toc_raw):
        if cursor + 18 > len(toc_raw):
            fail("ARCHIVE_TOC_INVALID")
        size = struct.unpack_from("!I", toc_raw, cursor)[0]
        if size < 19 or cursor + size > len(toc_raw):
            fail("ARCHIVE_TOC_INVALID")
        cursor += size
        count += 1
    if cursor != len(toc_raw) or count != len(archive.toc) + len(archive.options):
        fail("ARCHIVE_DUPLICATE_PATH")

    profile_path = build / "reviewed-runtime-config/game-protocol.json"
    profile = checked_profile(json.loads(profile_path.read_bytes(), object_pairs_hook=unique))
    profile_raw = json.dumps(profile_values(profile), separators=(",", ":")).encode("utf8")
    if profile_path.read_bytes() != profile_raw:
        fail("PROFILE_INVALID")
    parameters = [
        value.encode("utf8")
        for value in (profile.sdk_key, profile.development_code, profile.session_prefix)
    ]
    own = {"fgo_local_exporter/" + n: ROOT / "src/fgo_local_exporter" / n for n in STATIC_FILES}
    own.update({"fgo_stat_core/" + n: ROOT / "src/fgo_stat_core" / n for n in CORE_DATA})
    inventory = {}
    total = 0

    def record(name, kind, data, *, profile_allowed=False):
        nonlocal total
        if name in inventory:
            fail("DUPLICATE_PATH")
        total += len(data)
        if total > MAX_TOTAL or len(inventory) >= 20000:
            fail("ARCHIVE_LIMIT")
        if PRIVATE_KEY.search(data):
            fail("PRIVATE_KEY_FOUND")
        if name.lower().endswith((".js", ".css")) and re.search(
            rb"(?i)sourceMappingURL\s*=\s*data:", data
        ):
            fail("INLINE_SOURCE_MAP")
        scan_secret(data, secrets, context="python/" + name)
        if not profile_allowed and any(value in data for value in parameters):
            fail("PROFILE_OUTSIDE_ALLOWED_PATH")
        inventory[name] = {"type": kind, "size": len(data), "sha256": sha(data)}

    def nested_zip(data, prefix, depth=0):
        if depth > 3:
            fail("ARCHIVE_DEPTH")
        with zipfile.ZipFile(io.BytesIO(data)) as nested:
            names = nested.namelist()
            if len(set(names)) != len(names):
                fail("DUPLICATE_PATH")
            for item in nested.infolist():
                name_ok(item.filename)
                if item.is_dir():
                    continue
                if item.file_size > MAX_TOTAL:
                    fail("ARCHIVE_LIMIT")
                payload = nested.read(item)
                path = prefix + item.filename
                record(path, "zip-member", payload)
                if item.filename.endswith(".pyc"):
                    read_code(payload, pyc=True)
                if payload.startswith(b"MZ"):
                    check_pe(payload)
                if payload.startswith(b"PK\x03\x04"):
                    nested_zip(payload, path + "!/", depth + 1)

    record("!bootloader", "pe-bootloader", exe[: archive._start_offset])
    seen_names = set()
    for original_name, entry in archive.toc.items():
        name = original_name.replace("\\", "/")
        name_ok(name)
        if name in seen_names:
            fail("DUPLICATE_PATH")
        seen_names.add(name)
        kind = entry[4]
        data = archive.extract(original_name)
        if len(data) != entry[2]:
            fail("ARCHIVE_SIZE_INVALID")
        if name in own and data != own[name].read_bytes():
            fail("APPLICATION_RESOURCE_MISMATCH")
        if name.startswith(("fgo_local_exporter/", "fgo_stat_core/")) and name not in own:
            fail("APPLICATION_RESOURCE_UNREVIEWED")
        allowed_profile = name == "runtime-config/game-protocol.json"
        if allowed_profile and data != profile_raw:
            fail("PROFILE_MISMATCH")
        record(name, "carchive:" + kind, data, profile_allowed=allowed_profile)
        if kind in {"m", "M", "s"}:
            read_code(data)
        if data.startswith(b"MZ"):
            check_pe(data)
        if data.startswith(b"PK\x03\x04"):
            nested_zip(data, name + "!/")
        if kind == "z":
            pyz = archive.open_embedded_archive(original_name)
            for module, module_info in pyz.toc.items():
                name_ok(module.replace(".", "/"), module=True)
                code = pyz.extract(module)
                if code is None:
                    data = b""
                else:
                    if not isinstance(code, types.CodeType):
                        fail("PYZ_INVALID")
                    normalized(code, require_stripped=False)
                    data = marshal.dumps(code)
                record(name + "!/" + module, "pyz:" + str(module_info[0]), data)
    if not set(own) <= seen_names or "runtime-config/game-protocol.json" not in seen_names:
        fail("APPLICATION_RESOURCE_MISSING")
    return {
        "exe_sha256": sha(exe),
        "exe_size": len(exe),
        "options": archive.options,
        "inventory": inventory,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    if not re.fullmatch(r"[0-9a-f]{64}", args.manifest_sha256) or sha(raw) != args.manifest_sha256:
        fail("MANIFEST_HASH_INVALID")
    expected = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(expected, dict) or set(expected) != {
        "exe_sha256",
        "exe_size",
        "options",
        "inventory",
    }:
        fail("MANIFEST_INVALID")
    raw_secrets = sys.stdin.buffer.read(65537)
    if len(raw_secrets) > 65536:
        fail("SECRET_INPUT_INVALID")
    values = json.loads(raw_secrets or b"[]")
    if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
        fail("SECRET_INPUT_INVALID")
    secrets = [value.encode("utf8") for value in values]
    actual = inspect(args.executable, args.build, secrets)
    secrets.clear()
    if actual != expected:
        fail("INVENTORY_MISMATCH")
    print("DESKTOP_INVENTORY_REVIEW_PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        value = str(error)
        print(value if re.fullmatch(r"[A-Z_]{3,64}", value) else "DESKTOP_REVIEW_BLOCKED")
        raise SystemExit(2) from None
