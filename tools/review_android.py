"""复核 APK 的完整载荷、嵌套归档和编译模块；发现越界文件即拒绝发布。"""

import argparse
import base64
import hashlib
import io
import json
import marshal
import re
import stat
import struct
import subprocess
import sys
import types
import zipfile
import zlib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from prepare_android import (
    DATA,
    MODULES,
    ROOT,
    expected_stage_directories,
    expected_stage_files,
    read_resource_review,
    verify_inventory,
)
from release_parts.release_guard import require_source_free_release
from release_parts.runtime import CORE_DATA, CORE_MODULES

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.game_profile import checked_profile, profile_values
from fgo_local_exporter.reports import shared_report_revision


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def check_name(name):
    parts = PurePosixPath(name).parts
    if (
        name.startswith(("/", "\\"))
        or ".." in parts
        or "\\" in name
        or {"test", "tests", "__tests__", "SelfTest", ".git"}.intersection(parts)
        or name.endswith(
            (
                ".py",
                ".pyi",
                ".pyx",
                ".pxd",
                ".c",
                ".h",
                ".map",
                ".pdb",
                ".jks",
                ".keystore",
                ".p12",
                ".dpapi",
                ".env",
                ".log",
            )
        )
    ):
        raise RuntimeError("APK 中存在非运行文件：" + name)


def check_elf(raw, name):
    if raw[4:6] != b"\x02\x01":
        raise RuntimeError("APK 包含非预期架构：" + name)
    phoff = struct.unpack_from("<Q", raw, 32)[0]
    entry, count = struct.unpack_from("<HH", raw, 54)
    for i in range(count):
        offset = phoff + i * entry
        if struct.unpack_from("<I", raw, offset)[0] == 1:
            if struct.unpack_from("<Q", raw, offset + 48)[0] < 16384:
                raise RuntimeError("原生库不满足 16 KB 页对齐：" + name)
    shoff = struct.unpack_from("<Q", raw, 40)[0]
    size, count, index = struct.unpack_from("<HHH", raw, 58)
    start, length = struct.unpack_from("<QQ", raw, shoff + size * index + 24)
    strings = raw[start : start + length]
    for i in range(count):
        position = struct.unpack_from("<I", raw, shoff + i * size)[0]
        section = strings[position : strings.find(b"\0", position)]
        if section.startswith((b".debug_", b".zdebug_")):
            raise RuntimeError("原生库夹带调试符号：" + name)


def scan_archive(raw, *, prefix="", depth=0):
    if depth > 3:
        raise RuntimeError("APK 嵌套归档层数异常")
    files = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 12000:
            raise RuntimeError("APK 归档路径重复或数量异常")
        if sum(item.file_size for item in archive.infolist()) > 512 * 1024 * 1024:
            raise RuntimeError("APK 归档解压体积异常")
        for item in archive.infolist():
            check_name(item.filename)
            if item.is_dir():
                continue
            name = prefix + item.filename
            payload = archive.read(item)
            files[name] = digest(payload)
            if payload.startswith(b"PK\x03\x04"):
                files.update(scan_archive(payload, prefix=name + "!/", depth=depth + 1))
            if payload.startswith(b"\x7fELF"):
                check_elf(payload, name)
    return files


def review(
    apk_path,
    prepared,
    *,
    approved_pages=False,
    trusted=None,
    secret_values=(),
    java=None,
    apksigner_jar=None,
    aapt=None,
    dex_rebuilt_dir=None,
    zipalign=None,
    stage_review=None,
    resource_review=None,
):
    if approved_pages:
        return review_authorized_android(
            apk_path,
            prepared,
            trusted=trusted,
            secret_values=secret_values,
            java=java,
            apksigner_jar=apksigner_jar,
            aapt=aapt,
            dex_rebuilt_dir=dex_rebuilt_dir,
            zipalign=zipalign,
            stage_review=stage_review,
            resource_review=resource_review,
        )
    require_source_free_release()
    sources = {"fgo_local_exporter/" + name + ".py" for name in MODULES}
    sources.update("fgo_stat_core/" + name for name in CORE_MODULES)
    resources = {"fgo_local_exporter/" + name for name in DATA}
    resources.update("fgo_stat_core/" + name for name in CORE_DATA)
    staged = {
        path.relative_to(prepared / "python").as_posix()
        for path in (prepared / "python").rglob("*")
        if path.is_file()
    }
    if staged != sources | resources:
        raise RuntimeError("安卓输入目录与源码、资源白名单不符")
    for name in staged:
        if (prepared / "python" / name).read_bytes() != (ROOT / "src" / name).read_bytes():
            raise RuntimeError("构建材料不是当前审核版本：" + name)
    raw = apk_path.read_bytes()
    inventory = scan_archive(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as apk:
        expected_assets = {
            "assets/" + path.relative_to(prepared / "assets").as_posix(): digest(path.read_bytes())
            for path in (prepared / "assets").rglob("*")
            if path.is_file()
        }
        actual_assets = {
            name: digest(apk.read(name))
            for name in apk.namelist()
            if name.startswith("assets/")
            and not name.startswith("assets/chaquopy/")
            and not name.endswith("/")
        }
        if expected_assets != actual_assets:
            raise RuntimeError("APK 资源载荷与暂存白名单不符")
        if "assets/game-protocol.json" in actual_assets:
            checked_profile(json.loads(apk.read("assets/game-protocol.json")))
        with zipfile.ZipFile(io.BytesIO(apk.read("assets/chaquopy/app.imy"))) as app:
            expected = {name.removesuffix(".py") + ".pyc" for name in sources} | resources
            actual = {item.filename for item in app.infolist() if not item.is_dir()}
            if expected != actual:
                raise RuntimeError("APK 编译模块与审核清单不符")
            for name in resources:
                if app.read(name) != (prepared / "python" / name).read_bytes():
                    raise RuntimeError("APK 页面或资源哈希不符：" + name)
            for name in sources:
                code = marshal.loads(app.read(name[:-3] + ".pyc")[16:])
                source = (prepared / "python" / name).read_bytes()
                if code != compile(source, code.co_filename, "exec", dont_inherit=True):
                    raise RuntimeError("APK 编译内容不符：" + name)
        public = json.loads(apk.read("assets/public-manifest.json"))
        with zipfile.ZipFile(io.BytesIO(apk.read("assets/public-resources.zip"))) as archive:
            actual = {item.filename: digest(archive.read(item)) for item in archive.infolist()}
            if actual != public:
                raise RuntimeError("APK 公开资源集合与哈希不符")
    return {"apk_sha256": digest(raw), "apk_bytes": len(raw), "files": inventory}


# 审核签名、字节码及原生库；与构建和交付目录相互独立。
APK_MAGIC = b"APK Sig Block 42"
ALLOWED_SIGNERS = {0x7109871A, 0xF05368C0, 0x1B93AD61, 0x42726577}


class AuditError(Exception):
    pass


def fail(code):
    raise AuditError(code)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail("MANIFEST_DUPLICATE_KEY")
        value[key] = item
    return value


def normalized(code, *, require_stripped):
    if not isinstance(code, types.CodeType):
        fail("PYC_INVALID")
    if require_stripped and (
        code.co_filename != "<release>" or code.co_linetable or code.co_firstlineno != 1
    ):
        fail("PYC_DEBUG_METADATA")

    def clean_constant(item):
        if isinstance(item, types.CodeType):
            return normalized(item, require_stripped=require_stripped)
        if isinstance(item, tuple):
            return tuple(clean_constant(value) for value in item)
        if isinstance(item, frozenset):
            return frozenset(clean_constant(value) for value in item)
        if isinstance(item, (list, dict, set)):
            fail("PYC_INVALID")
        return item

    return code.replace(
        co_filename="<release>",
        co_linetable=b"",
        co_firstlineno=1,
        co_consts=tuple(clean_constant(item) for item in code.co_consts),
    )


def read_stripped_pyc(data):
    if len(data) < 16:
        fail("PYC_INVALID")
    from importlib.util import MAGIC_NUMBER

    if data[:4] != MAGIC_NUMBER or data[4:16] != b"\0" * 12:
        fail("PYC_VERSION_INVALID")
    try:
        stream = io.BytesIO(data[16:])
        code = marshal.load(stream)
        if stream.read(1):
            fail("PYC_TRAILING_DATA")
        normalized(code, require_stripped=True)
        return code
    except (EOFError, ValueError, TypeError):
        fail("PYC_INVALID")


def verify_python_module(data, source, expected_source):
    if digest(source) != expected_source:
        fail("PYC_SOURCE_MISMATCH")
    try:
        code = read_stripped_pyc(data)
        expected = compile(source, "<reviewed>", "exec", dont_inherit=True)
        if normalized(code, require_stripped=True) != normalized(expected, require_stripped=False):
            fail("PYC_SOURCE_MISMATCH")
    except (ValueError, TypeError, SyntaxError):
        fail("PYC_INVALID")


class SigningAlias(bytes):
    """A keystore entry name, screened as a complete token rather than a substring."""


def _public_repository_alias_offset(data, alias, context):
    prefix = b"https://github.com/ayamedog/"
    if context == "classes.dex":
        java = _read_source(
            ROOT / "android/app/src/main/java/io/github/ayamedog/fgostats/MainActivity.java"
        )
        url = prefix + alias
        constant = b'SOURCE_REPOSITORY = "' + url + b'";'
        if (java.count(constant) != 1 or data.count(url) != 1 or len(data) < 112
                or not data.startswith(b"dex\n") or not url.isascii()):
            return None
        count, offset = struct.unpack_from("<II", data, 56)
        if count > 1_000_000 or offset > len(data) or count * 4 > len(data) - offset:
            return None
        matches = []
        for index in range(count):
            position = struct.unpack_from("<I", data, offset + index * 4)[0]
            length = 0
            for shift in range(0, 35, 7):
                if position >= len(data):
                    return None
                item = data[position]
                position += 1
                length |= (item & 0x7F) << shift
                if item < 0x80:
                    break
            else:
                return None
            if (length == len(url) and data[position:position + len(url) + 1] == url + b"\0"):
                matches.append(position + len(prefix))
        return matches[0] if len(matches) == 1 else None

    page = "fgo_local_exporter/ui/index.html"
    if context not in {"python/" + page, APP_ARCHIVE + "!/" + page}:
        return None
    if data != _read_source(ROOT / "src" / page):
        return None

    class RepositoryParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []

        def handle_starttag(self, tag, attrs):
            if tag == "a" and ("id", "source-repository") in attrs:
                self.urls.extend(value for name, value in attrs if name == "href")

    try:
        url = (prefix + alias).decode("utf-8")
        parser = RepositoryParser()
        parser.feed(data.decode("utf-8"))
    except UnicodeError:
        return None
    href = b'href="' + prefix + alias + b'"'
    if parser.urls != [url] or data.count(href) != 1:
        return None
    return data.index(href) + len(b'href="' + prefix)


def scan_secret(data, forbidden, *, context=None):
    for value in forbidden:
        candidates = {
            value,
            base64.b64encode(value),
            base64.urlsafe_b64encode(value),
            value.hex().encode("ascii"),
            value.hex().upper().encode("ascii"),
        }
        try:
            decoded = value.decode("utf-8")
            candidates.add(decoded.encode("utf-16le"))
            candidates.add(decoded.encode("utf-16be"))
        except UnicodeError:
            pass
        if isinstance(value, SigningAlias):
            token = rb"A-Za-z0-9_.-"
            public_offset = _public_repository_alias_offset(data, value, context)
            for candidate in candidates:
                if candidate:
                    pattern = (
                        rb"(?<![" + token + rb"])" + re.escape(candidate)
                        + rb"(?![" + token + rb"])"
                    )
                    for match in re.finditer(pattern, data):
                        if candidate != value or match.start() != public_offset:
                            fail("SECRET_FOUND")
        elif any(candidate and candidate in data for candidate in candidates):
            fail("SECRET_FOUND")


def check_signing(data, central, expected):
    if central < 32 or data[central - 16 : central] != APK_MAGIC:
        fail("APK_SIGNATURE_BLOCK_MISSING")
    length = struct.unpack_from("<Q", data, central - 24)[0]
    start = central - length - 8
    if length < 24 or start < 0 or struct.unpack_from("<Q", data, start)[0] != length:
        fail("APK_SIGNATURE_BLOCK_INVALID")
    cursor, end = start + 8, central - 24
    seen = {}
    while cursor < end:
        if cursor + 8 > end:
            fail("APK_SIGNATURE_BLOCK_INVALID")
        size = struct.unpack_from("<Q", data, cursor)[0]
        cursor += 8
        if size < 4 or cursor + size > end:
            fail("APK_SIGNATURE_BLOCK_INVALID")
        ident = struct.unpack_from("<I", data, cursor)[0]
        if ident not in ALLOWED_SIGNERS or ident in seen:
            fail("APK_SIGNATURE_BLOCK_UNREVIEWED")
        value = data[cursor + 4 : cursor + size]
        if ident == 0x42726577 and any(value):
            fail("APK_SIGNATURE_PADDING_INVALID")
        seen[f"{ident:08x}"] = {"size": len(value), "sha256": digest(value)}
        cursor += size
    if cursor != end or not (set(seen) & {"7109871a", "f05368c0", "1b93ad61"}):
        fail("APK_SIGNATURE_BLOCK_INVALID")
    if seen != expected:
        fail("APK_SIGNATURE_BLOCK_MISMATCH")
    return start


def check_eocd(data):
    if len(data) < 22 or data[-22:-18] != b"PK\x05\x06":
        fail("ZIP_TRAILING_OR_COMMENT")
    fields = struct.unpack_from("<IHHHHIIH", data, len(data) - 22)
    if fields[1] or fields[2] or fields[3] != fields[4] or fields[7]:
        fail("ZIP_UNSUPPORTED")
    if fields[5] + fields[6] != len(data) - 22:
        fail("ZIP_DIRECTORY_INVALID")
    return fields[6]


def verify_elf(data, path):
    if len(data) < 64 or data[4:6] != b"\x02\x01":
        fail("ELF_INVALID")
    try:
        machine = struct.unpack_from("<H", data, 18)[0]
        if machine not in {62, 183} or struct.unpack_from("<H", data, 16)[0] != 3:
            fail("ELF_ABI_INVALID")
        if (machine == 62 and "x86_64" not in path) or (
            machine == 183 and "arm64-v8a" not in path and "aarch64" not in path
        ):
            fail("ELF_ABI_INVALID")
        phoff, shoff = struct.unpack_from("<QQ", data, 32)
        phsize, phnum, shsize, shnum, shstr = struct.unpack_from("<HHHHH", data, 54)
        if (
            phsize < 56
            or shsize < 64
            or not 0 < phnum <= 1024
            or not 0 < shnum <= 8192
            or shstr >= shnum
        ):
            fail("ELF_INVALID")
        loads = 0
        for index in range(phnum):
            offset = phoff + phsize * index
            if struct.unpack_from("<I", data, offset)[0] == 1:
                loads += 1
                file_offset, address = struct.unpack_from("<QQ", data, offset + 8)
                file_size, memory_size, alignment = struct.unpack_from("<QQQ", data, offset + 32)
                if (
                    alignment < 16384
                    or alignment & (alignment - 1)
                    or file_offset % alignment != address % alignment
                    or file_size > memory_size
                    or file_offset + file_size > len(data)
                ):
                    fail("ELF_ALIGNMENT_INVALID")
        if not loads:
            fail("ELF_INVALID")
        strings_offset, strings_size = struct.unpack_from("<QQ", data, shoff + shsize * shstr + 24)
        if strings_offset + strings_size > len(data):
            fail("ELF_INVALID")
        strings = data[strings_offset : strings_offset + strings_size]
        for index in range(shnum):
            offset = shoff + shsize * index
            name_index, section_type = struct.unpack_from("<II", data, offset)
            if name_index >= len(strings):
                fail("ELF_INVALID")
            end = strings.find(b"\0", name_index)
            if end < 0:
                fail("ELF_INVALID")
            name = strings[name_index:end]
            if section_type == 2 or name.startswith((b".debug_", b".zdebug_", b".gnu_debuglink")):
                fail("ELF_DEBUG_SYMBOLS")
    except (struct.error, OverflowError):
        fail("ELF_INVALID")


def verify_dex(data, expected):
    if len(data) < 112 or not re.fullmatch(rb"dex\n0(?:35|37|38|39|40|41)\x00", data[:8]):
        fail("DEX_INVALID")
    try:
        if (
            struct.unpack_from("<I", data, 32)[0] != len(data)
            or struct.unpack_from("<I", data, 36)[0] != 112
            or struct.unpack_from("<I", data, 40)[0] != 0x12345678
            or hashlib.sha1(data[32:]).digest() != data[12:32]
            or zlib.adler32(data[12:]) & 0xFFFFFFFF != struct.unpack_from("<I", data, 8)[0]
        ):
            fail("DEX_INVALID")
        string_count, string_offset = struct.unpack_from("<II", data, 56)
        type_count, type_offset = struct.unpack_from("<II", data, 64)
        class_count, class_offset = struct.unpack_from("<II", data, 96)
        map_offset = struct.unpack_from("<I", data, 52)[0]
        if (
            string_count > 1000000
            or type_count > 65536
            or class_count > 65536
            or map_offset < 112
            or map_offset + 4 > len(data)
            or string_offset + 4 * string_count > len(data)
            or type_offset + 4 * type_count > len(data)
            or class_offset + 32 * class_count > len(data)
        ):
            fail("DEX_INVALID")
        map_count = struct.unpack_from("<I", data, map_offset)[0]
        if map_count > 10000 or map_offset + 4 + 12 * map_count > len(data):
            fail("DEX_INVALID")
        map_types = set()
        for index in range(map_count):
            item_type, reserved, count, item_offset = struct.unpack_from(
                "<HHII", data, map_offset + 4 + 12 * index
            )
            if reserved or item_type in map_types or not count or item_offset >= len(data):
                fail("DEX_INVALID")
            if item_type == 0x2003:
                fail("DEX_DEBUG_METADATA")
            map_types.add(item_type)

        def uleb(cursor):
            value = 0
            for shift in range(0, 35, 7):
                if cursor >= len(data):
                    fail("DEX_INVALID")
                byte = data[cursor]
                cursor += 1
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    return value, cursor
            fail("DEX_INVALID")

        classes = []
        for index in range(class_count):
            class_def = class_offset + 32 * index
            type_index = struct.unpack_from("<I", data, class_def)[0]
            source_file_index = struct.unpack_from("<I", data, class_def + 16)[0]
            class_data_offset = struct.unpack_from("<I", data, class_def + 24)[0]
            if source_file_index != 0xFFFFFFFF:
                fail("DEX_DEBUG_METADATA")
            if type_index >= type_count:
                fail("DEX_INVALID")
            string_index = struct.unpack_from("<I", data, type_offset + 4 * type_index)[0]
            if string_index >= string_count:
                fail("DEX_INVALID")
            cursor = struct.unpack_from("<I", data, string_offset + 4 * string_index)[0]
            _length, cursor = uleb(cursor)
            end = data.find(b"\0", cursor, min(cursor + 4096, len(data)))
            if end < 0:
                fail("DEX_INVALID")
            name = data[cursor:end].decode("ascii")
            if not re.fullmatch(r"L[A-Za-z0-9_$/-]+;", name):
                fail("DEX_INVALID")
            classes.append(name)
            if class_data_offset:
                cursor = class_data_offset
                counts = []
                for _ in range(4):
                    count, cursor = uleb(cursor)
                    if count > 65536:
                        fail("DEX_INVALID")
                    counts.append(count)
                for _ in range(counts[0] + counts[1]):
                    _field_index, cursor = uleb(cursor)
                    _field_flags, cursor = uleb(cursor)
                for _ in range(counts[2] + counts[3]):
                    _method_index, cursor = uleb(cursor)
                    _method_flags, cursor = uleb(cursor)
                    code_offset, cursor = uleb(cursor)
                    if code_offset and (
                        code_offset + 16 > len(data)
                        or struct.unpack_from("<I", data, code_offset + 8)[0]
                    ):
                        fail("DEX_DEBUG_METADATA")
        if len(classes) != len(set(classes)) or sorted(classes) != expected:
            fail("DEX_CLASS_MISMATCH")
    except (struct.error, UnicodeError, OverflowError):
        fail("DEX_INVALID")


def verify_signature(java, apksigner_jar, apk, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    result = subprocess.run(
        [str(java), "-jar", str(apksigner_jar), "verify", "--verbose", "--print-certs", str(apk)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    if result.returncode:
        return False
    text = result.stdout.decode("utf8", errors="replace")
    if not re.search(r"(?im)^Verified using v[23](?:\.\d+)? scheme[^:]*:\s*true\s*$", text):
        return False
    certificates = re.findall(
        r"(?im)^Signer #\d+ certificate SHA-256 digest: ([0-9a-f]{64})\s*$", text
    )
    return len(certificates) == 1 and certificates[0].lower() == expected


def verify_android_metadata(aapt, apk, expected):
    required = {
        "application_id",
        "version_code",
        "version_name",
        "min_sdk",
        "target_sdk",
        "permissions",
    }
    if not isinstance(expected, dict) or set(expected) != required:
        return False
    result = subprocess.run(
        [str(aapt), "dump", "badging", str(apk)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    if result.returncode:
        return False
    value = result.stdout.decode("utf8", errors="replace").replace("\r\n", "\n")
    package = re.findall(
        r"(?m)^package: name='([^']+)' versionCode='([^']+)' versionName='([^']*)'",
        value,
    )
    minimum = re.findall(r"(?m)^sdkVersion:'([^']+)'", value)
    target = re.findall(r"(?m)^targetSdkVersion:'([^']+)'", value)
    permissions = re.findall(r"(?m)^uses-permission(?:-sdk-\d+)?: name='([^']+)'", value)
    permission_lines = re.findall(r"(?m)^uses-permission(?:-sdk-\d+)?:.*$", value)
    implied_lines = re.findall(r"(?m)^uses-implied-permission:.*$", value)
    if not (
        len(package) == 1
        and package[0]
        == (expected["application_id"], expected["version_code"], expected["version_name"])
        and minimum == [expected["min_sdk"]]
        and target == [expected["target_sdk"]]
        and sorted(permissions) == [
            "android.permission.INTERNET",
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
        ]
        and sorted(permission_lines) == [
            "uses-permission: name='android.permission.INTERNET'",
            "uses-permission: name='android.permission.READ_EXTERNAL_STORAGE' maxSdkVersion='28'",
            "uses-permission: name='android.permission.WRITE_EXTERNAL_STORAGE' maxSdkVersion='28'",
        ]
        and implied_lines == [
            "uses-implied-permission: name='android.permission.READ_EXTERNAL_STORAGE' "
            "maxSdkVersion='28' reason='requested WRITE_EXTERNAL_STORAGE'"
        ]
        and expected["permissions"] == [
            "android.permission.INTERNET", "android.permission.WRITE_EXTERNAL_STORAGE"
        ]
    ):
        return False
    manifest = subprocess.run(
        [str(aapt), "dump", "xmltree", str(apk), "AndroidManifest.xml"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    if manifest.returncode:
        return False
    tree = manifest.stdout.decode("utf8", errors="replace").replace("\r\n", "\n")
    nodes = re.findall(
        r"(?m)^    E: (uses-permission(?:-sdk-\d+)?) \(line=\d+\)\r?\n"
        r"((?:^      A: [^\r\n]*\r?\n)*)",
        tree,
    )
    all_permission_nodes = re.findall(r"(?m)^\s*E: uses-permission(?:-sdk-\d+)?\b", tree)
    if len(nodes) != 2 or len(all_permission_nodes) != 2:
        return False
    observed = []
    for tag, block in nodes:
        attributes = re.findall(r"(?m)^      A: android:([^ (]+)\([^)]*\)=(.*)$", block)
        if tag != "uses-permission" or len(attributes) != len(block.splitlines()):
            return False
        fields = dict(attributes)
        if len(fields) != len(attributes):
            return False
        name = fields.get("name", "")
        if name == '"android.permission.INTERNET" (Raw: "android.permission.INTERNET")':
            if set(fields) != {"name"}:
                return False
            observed.append("android.permission.INTERNET")
        elif name == ('"android.permission.WRITE_EXTERNAL_STORAGE" '
                      '(Raw: "android.permission.WRITE_EXTERNAL_STORAGE")'):
            if (set(fields) != {"name", "maxSdkVersion"}
                    or fields["maxSdkVersion"] != "(type 0x10)0x1c"):
                return False
            observed.append("android.permission.WRITE_EXTERNAL_STORAGE")
        else:
            return False
    return sorted(observed) == expected["permissions"]


# 仅本次 Android 授权的运行页面。改变此集合必须重新取得对应授权。
AUTHORIZED_PAGES = frozenset(
    {
        "fgo_local_exporter/ui/index.html",
        "fgo_local_exporter/ui/style.css",
        "fgo_local_exporter/ui/app.js",
        "fgo_local_exporter/ui/gacha-records.js",
        "fgo_local_exporter/ui/gacha-records.css",
        "fgo_local_exporter/ui/html-to-image.js",
        "fgo_local_exporter/ui/resource-status.js",
        "fgo_local_exporter/ui/captcha-frame.html",
        "fgo_local_exporter/ui/captcha-frame.js",
        "fgo_local_exporter/desktop_static/setup.html",
        "fgo_local_exporter/desktop_static/desktop.css",
        "fgo_local_exporter/mobile_static/mobile.js",
        "fgo_local_exporter/mobile_static/mobile.css",
        "fgo_stat_core/infrastructure/report_ui/template.html",
    }
)
PROFILE_ASSET = "assets/game-protocol.json"
APP_ARCHIVE = "assets/chaquopy/app.imy"
MAX_APK_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 16000
FONT_HASHES = {
    "fonts/NotoSansCJK-Regular.ttc": (
        "d18a3adba5c891eb7f9cdf8c31dad28dcc324db80e662d6a0f91e8db6aee5913"
    ),
    "fonts/DejaVuSans.ttf": "f9a7bd58cdaa9d1e9acafd070db8dea0b6db9cf2b540501b15075315a47cba3d",
}
FONT_LICENSE_HASHES = {
    "licenses/NotoSansCJK-OFL-1.1.txt": (
        "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
    ),
    "licenses/DejaVu-Fonts.txt": "7a083b136e64d064794c3419751e5c7dd10d2f64c108fe5ba161eae5e5958a93",
    "licenses/Font-Notices.txt": "98391ea4a8f22702de0d88d167425441eae64e464e4d439903d44474b501e08d",
    "licenses/Android-Dependency-Notices.txt": (
        "a11241c8a88e9831483c42e0eb5807f9397fc10c4bdfe5552e9685957d8727ed"
    ),
    "licenses/Android-Rust-Dependency-Notices.txt": (
        "d33ad040b86b3697dafcb3a1397dc18175a8d6e154c8cc1f58f646f1c083d1f2"
    ),
    "licenses/MPL-2.0.txt": "3f3d9e0024b1921b067d6f7f88deb4a60cbe7a78e76c64e3f1d7fc3b779b9d04",
}


def _record(payload, kind="file"):
    return {"kind": kind, "size": len(payload), "sha256": digest(payload)}


def _no_links(path):
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            fail("PATH_LINK_REJECTED")


def _name_allowed(name, full_name, authorized):
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name or "!/" in name:
        fail("PATH_INVALID")
    directory = name.endswith("/")
    parts = (name[:-1] if directory else name).split("/")
    if name.startswith("/") or any(
        not part or part in {".", ".."} or ":" in part for part in parts
    ):
        fail("PATH_INVALID")
    if any(
        part.casefold() in {"test", "tests", "__tests__", "selftest", "fixtures", ".git"}
        for part in parts
    ):
        fail("SOURCE_OR_PRIVATE_FILE")
    lower = name.lower()
    if not directory and lower.endswith(
        (
            ".py",
            ".pyi",
            ".pyx",
            ".pxd",
            ".c",
            ".h",
            ".java",
            ".kt",
            ".map",
            ".pdb",
            ".jks",
            ".keystore",
            ".p12",
            ".pfx",
            ".dpapi",
            ".env",
            ".log",
            ".har",
            ".pcap",
            ".db",
            ".db3",
            ".sqlite",
            ".sqlite3",
            ".jar",
            ".aar",
            ".class",
        )
    ):
        fail("SOURCE_OR_PRIVATE_FILE")
    if (
        not directory
        and lower.endswith((".html", ".htm", ".js", ".mjs", ".cjs", ".css"))
        and full_name not in authorized
    ):
        fail("UNAUTHORIZED_PAGE_SOURCE")
    if (
        not directory
        and parts[-1].casefold() == "game-protocol.json"
        and full_name != PROFILE_ASSET
    ):
        fail("PROFILE_SCOPE_INVALID")
    return directory


def _project_probes():
    roots = (ROOT / "src", ROOT / "android/app/src/main/java", ROOT / "tools", ROOT / "tests")
    probes, total = set(), 0
    for root in roots:
        _no_links(root)
        if not root.is_dir():
            fail("SOURCE_ROOT_INVALID")
        for path in root.rglob("*"):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                fail("SOURCE_ROOT_INVALID")
            if not path.is_file() or path.suffix.lower() not in {
                ".py",
                ".java",
                ".kt",
                ".html",
                ".js",
                ".css",
            }:
                continue
            size = path.stat().st_size
            total += size
            if size > 4 * 1024 * 1024 or total > 32 * 1024 * 1024:
                fail("SOURCE_ROOT_LIMIT")
            raw = path.read_bytes()
            for source in {raw, raw.replace(b"\r\n", b"\n")}:
                if len(source) >= 80:
                    probes.add(source)
                if len(source) >= 256:
                    probes.update(
                        source[offset : offset + 256] for offset in range(0, len(source) - 255, 256)
                    )
    if not probes:
        fail("SOURCE_ROOT_INVALID")
    return probes


def _scan_source(payload, probes, *, allowed_span=None):
    for probe in probes:
        start = 0
        while (position := payload.find(probe, start)) >= 0:
            end = position + len(probe)
            if allowed_span is None or position < allowed_span[0] or end > allowed_span[1]:
                fail("PROJECT_SOURCE_FOUND")
            start = position + 1


REPORT_STYLE_SHA256 = "a4ce0b74314a2d127e99ea1ca6ea9956d580c84ae6b1cdbe53e9e0a7f0652c6a"
ZIPIMPORT_SHA256 = "992a61b28c55515d814f538fd20c791b2ef76abc7ac3e83a4d7c24ee4d2f4afd"


def _verified_report_css_span(content, full, prepared, sources):
    name = "fgo_stat_core/infrastructure/report_ui/builder.pyc"
    if full != APP_ARCHIVE + "!/" + name:
        return None
    source = sources.get(name)
    if source is None or content != (prepared / "python" / name).read_bytes():
        fail("APP_CONTENT_MISMATCH")
    original = _read_source(source)
    verify_python_module(content, original, digest(original))
    root = read_stripped_pyc(content)
    styles = []

    def visit(code):
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                visit(value)
            elif code.co_name == "build" and isinstance(value, str):
                raw = value.encode("utf8")
                if digest(raw) == REPORT_STYLE_SHA256:
                    styles.append(raw)

    visit(root)
    if len(styles) != 1 or content.count(styles[0]) != 1:
        fail("REPORT_STYLE_MISMATCH")
    start = content.index(styles[0])
    return start, start + len(styles[0])


def _verified_zipimport_marker(content, full):
    if (full != "assets/chaquopy/stdlib-common.imy!/zipimport.pyc"
            or digest(content) != ZIPIMPORT_SHA256):
        return False
    read_stripped_pyc(content)
    try:
        with zipfile.ZipFile(io.BytesIO(content)):
            return False
    except zipfile.BadZipFile:
        return True


def _verify_zip_extras(name, depth, compression, header_offset, name_length, local, central):
    if central:
        fail("ZIP_EXTRA_UNREVIEWED")
    if depth:
        if local:
            fail("ZIP_EXTRA_UNREVIEWED")
        return
    if compression != 0:
        if local:
            fail("ZIP_EXTRA_UNREVIEWED")
        return
    alignment = 16384 if name.lower().endswith(".so") else 4
    padding = (-(header_offset + 30 + name_length)) % alignment
    if len(local) != padding or any(local):
        fail("ZIP_EXTRA_UNREVIEWED")


def _scan_private_patterns(payload):
    patterns = (
        rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/]{32}",
        rb'(?i)["\'](?:access_token|access_key|refresh_token|usk|session_id|cookie|password)["\']\s*:\s*["\'][A-Za-z0-9_./+=-]{16,}["\']',
    )
    if any(re.search(pattern, payload) for pattern in patterns):
        fail("PRIVATE_DATA_FOUND")


def _tree(prepared, expected, profile_secret, extra_secret, *, approved_apk=None):
    _no_links(prepared)
    if not prepared.is_dir():
        fail("STAGE_INVALID")
    seen, identities, total = {}, {}, 0
    for path in prepared.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            fail("STAGE_LINK_REJECTED")
        relative = path.relative_to(prepared).as_posix()
        info = path.stat(follow_symlinks=False)
        identity = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
        if path.is_dir():
            seen[relative + "/"] = _record(b"", "directory")
            identities[relative + "/"] = identity
        elif path.is_file():
            if info.st_nlink != 1 or info.st_size > MAX_APK_BYTES:
                fail("STAGE_INVALID")
            raw = path.read_bytes()
            after = path.stat(follow_symlinks=False)
            if identity != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
            ):
                fail("STAGE_CHANGED")
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                fail("STAGE_LIMIT")
            if relative != PROFILE_ASSET and relative != approved_apk:
                scan_secret(raw, profile_secret)
            if relative != approved_apk:
                scan_secret(raw, extra_secret, context=relative)
                _scan_private_patterns(raw)
            seen[relative] = _record(raw)
            identities[relative] = identity
        else:
            fail("STAGE_INVALID")
        if len(seen) > MAX_MEMBERS:
            fail("STAGE_LIMIT")
    if seen != expected:
        fail("STAGE_MANIFEST_MISMATCH")
    return identities


def _source_maps():
    result = {
        "fgo_local_exporter/" + name + ".pyc": ROOT / "src/fgo_local_exporter" / (name + ".py")
        for name in MODULES
    }
    result.update(
        {
            "fgo_stat_core/" + name.removesuffix(".py") + ".pyc": ROOT / "src/fgo_stat_core" / name
            for name in CORE_MODULES
        }
    )
    return result


def _read_source(path):
    _no_links(path)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        fail("SOURCE_INVALID")
    return path.read_bytes()


def _verify_prepared(prepared, source_hashes):
    staged_files = {
        path.relative_to(prepared).as_posix() for path in prepared.rglob("*") if path.is_file()
    }
    staged_directories = {
        path.relative_to(prepared).as_posix() for path in prepared.rglob("*") if path.is_dir()
    }
    if staged_files != expected_stage_files() or staged_directories != expected_stage_directories():
        fail("STAGE_SCOPE_INVALID")
    try:
        engine = json.loads(
            (prepared / "assets/report-engine.json").read_bytes(), object_pairs_hook=unique_object
        )
    except (ValueError, UnicodeError):
        fail("REPORT_ENGINE_MISMATCH")
    if engine != {"version": 1, "revision": shared_report_revision(ROOT / "src")}:
        fail("REPORT_ENGINE_MISMATCH")
    for source, target in (
        ("LICENSE", "project-MIT.txt"),
        ("licenses/html-to-image-MIT.txt", "html-to-image-MIT.txt"),
        ("licenses/Chaquopy-MIT.txt", "Chaquopy-MIT.txt"),
        ("licenses/Python-3.13.txt", "Python-3.13.txt"),
        ("licenses/OpenSSL-Apache-2.0.txt", "OpenSSL-Apache-2.0.txt"),
        ("licenses/NotoSansCJK-OFL-1.1.txt", "NotoSansCJK-OFL-1.1.txt"),
        ("licenses/DejaVu-Fonts.txt", "DejaVu-Fonts.txt"),
        ("licenses/Font-Notices.txt", "Font-Notices.txt"),
        ("licenses/Android-Dependency-Notices.txt", "Android-Dependency-Notices.txt"),
        ("licenses/Android-Rust-Dependency-Notices.txt", "Android-Rust-Dependency-Notices.txt"),
        ("licenses/MPL-2.0.txt", "MPL-2.0.txt"),
    ):
        original = _read_source(ROOT / source)
        if (prepared / "assets/licenses" / target).read_bytes() != original or (
            source in FONT_LICENSE_HASHES and digest(original) != FONT_LICENSE_HASHES[source]
        ):
            fail("LICENSE_MISMATCH")
    data = {"fgo_local_exporter/" + name: ROOT / "src/fgo_local_exporter" / name for name in DATA}
    data.update({"fgo_stat_core/" + name: ROOT / "src/fgo_stat_core" / name for name in CORE_DATA})
    pages = {name for name in data if name.endswith((".html", ".js", ".css"))}
    if pages != AUTHORIZED_PAGES:
        fail("AUTHORIZED_PAGE_SCOPE_INVALID")
    sources = _source_maps()
    expected_sources = {
        str(path.relative_to(ROOT / "src")).replace("\\", "/"): digest(_read_source(path))
        for path in (*data.values(), *sources.values())
    }
    if expected_sources != source_hashes:
        fail("SOURCE_HASH_MISMATCH")
    root = prepared / "python"
    observed = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if observed != set(data) | set(sources):
        fail("PYTHON_STAGE_SCOPE_INVALID")
    for name, source in data.items():
        if (root / name).read_bytes() != _read_source(source):
            fail("AUTHORIZED_RESOURCE_MISMATCH")
    for name, source in sources.items():
        content = _read_source(source)
        verify_python_module((root / name).read_bytes(), content, digest(content))
    return data, sources


def _scan_zip(
    raw,
    prefix,
    depth,
    actual,
    authorized,
    prepared,
    sources,
    dex_classes,
    signing,
    profile_secret,
    extra_secret,
    probes,
    totals,
):
    if depth > 4 or len(raw) > MAX_APK_BYTES:
        fail("ARCHIVE_LIMIT")
    central = check_eocd(raw)
    boundary = check_signing(raw, central, signing) if depth == 0 else central
    if depth == 0:
        scan_secret(raw[boundary:central], profile_secret + extra_secret)
        _scan_private_patterns(raw[boundary:central])
        _scan_source(raw[boundary:central], probes)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError, ValueError):
        fail("ARCHIVE_INVALID")
    with archive:
        if archive.start_dir != central or archive.comment:
            fail("ZIP_DIRECTORY_INVALID")
        seen, folded, ranges = set(), set(), []
        for item in archive.infolist():
            name, full = item.filename, prefix + item.filename
            directory = _name_allowed(name, full, authorized)
            if name in seen or name.casefold() in folded:
                fail("ZIP_DUPLICATE_NAME")
            seen.add(name)
            folded.add(name.casefold())
            if item.flag_bits & ~0x800 or item.compress_type not in {0, 8} or item.comment:
                fail("ZIP_MEMBER_UNSUPPORTED")
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or (
                mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
            ):
                fail("ZIP_MEMBER_TYPE_INVALID")
            if item.file_size > MAX_APK_BYTES or item.compress_size > MAX_APK_BYTES:
                fail("ARCHIVE_LIMIT")
            try:
                header = struct.unpack_from("<IHHHHHIIIHH", raw, item.header_offset)
                magic, _version, flags, compression, _time, _date, crc, packed, size, n, e = header
                name_start = item.header_offset + 30
                name_end = name_start + n
                extra_end = name_end + e
                local_name = raw[name_start:name_end].decode("utf8" if flags & 0x800 else "cp437")
                if (
                    magic != 0x04034B50
                    or local_name != name
                    or flags != item.flag_bits
                    or compression != item.compress_type
                    or crc != item.CRC
                    or packed != item.compress_size
                    or size != item.file_size
                ):
                    fail("ZIP_LOCAL_HEADER_MISMATCH")
                end = extra_end + packed
                if end > boundary:
                    fail("ZIP_LOCAL_HEADER_MISMATCH")
                local_extra = raw[name_end:extra_end]
                content = archive.read(item)
            except (struct.error, UnicodeError, zipfile.BadZipFile, RuntimeError, OSError):
                fail("ZIP_MEMBER_INVALID")
            ranges.append((item.header_offset, end))
            _verify_zip_extras(name, depth, item.compress_type, item.header_offset, n,
                               local_extra, item.extra)
            record = _record(content, "directory" if directory else "file")
            record.update(
                {
                    "local_extra_sha256": digest(local_extra),
                    "central_extra_sha256": digest(item.extra),
                }
            )
            if directory and content:
                fail("ZIP_DIRECTORY_INVALID")
            if full in actual:
                fail("ZIP_DUPLICATE_NAME")
            actual[full] = record
            totals["size"] += len(content)
            totals["count"] += 1
            if totals["size"] > MAX_TOTAL_BYTES or totals["count"] > MAX_MEMBERS:
                fail("ARCHIVE_LIMIT")
            metadata = name.encode("utf8") + local_extra + item.extra
            scan_secret(metadata, profile_secret + extra_secret)
            _scan_private_patterns(metadata)
            _scan_source(metadata, probes)
            is_container = content.startswith((b"PK\x03\x04", b"PK\x05\x06"))
            if not is_container:
                scan_secret(content, extra_secret, context=full)
                if full != PROFILE_ASSET:
                    scan_secret(content, profile_secret)
                _scan_private_patterns(content)
                if full not in authorized:
                    span = None
                    if (full == APP_ARCHIVE + "!/fgo_stat_core/infrastructure/report_ui/builder.pyc"
                            and any(probe in content for probe in probes)):
                        span = _verified_report_css_span(content, full, prepared, sources)
                    _scan_source(content, probes, allowed_span=span)
            if full == PROFILE_ASSET and content != (prepared / PROFILE_ASSET).read_bytes():
                fail("PROFILE_MISMATCH")
            if full in authorized:
                name_in_app = full.removeprefix(APP_ARCHIVE + "!/")
                if content != (prepared / "python" / name_in_app).read_bytes():
                    fail("AUTHORIZED_RESOURCE_MISMATCH")
            if full.startswith(APP_ARCHIVE + "!/") and not directory:
                app_name = full.removeprefix(APP_ARCHIVE + "!/")
                if app_name in sources:
                    source = _read_source(sources[app_name])
                    verify_python_module(content, source, digest(source))
                elif app_name not in {"fgo_local_exporter/" + value for value in DATA} | {
                    "fgo_stat_core/" + value for value in CORE_DATA
                }:
                    fail("APP_MODULE_UNREVIEWED")
                if content != (prepared / "python" / app_name).read_bytes():
                    fail("APP_CONTENT_MISMATCH")
            if full.lower().endswith(".pyc"):
                read_stripped_pyc(content)
            if full.lower().endswith(".so") and not content.startswith(b"\x7fELF"):
                fail("ELF_INVALID")
            if content.startswith(b"\x7fELF"):
                verify_elf(content, full)
            if full.lower().endswith(".dex"):
                if full not in dex_classes or not isinstance(dex_classes[full], list):
                    fail("DEX_UNREVIEWED")
                verify_dex(content, dex_classes[full])
            if name.lower().endswith((".imy", ".zip")) and not is_container:
                fail("ARCHIVE_INVALID")
            if (
                not is_container
                and b"PK\x05\x06" in content[-65557:]
                and zipfile.is_zipfile(io.BytesIO(content))
                and not _verified_zipimport_marker(content, full)
            ):
                fail("UNREVIEWED_ARCHIVE")
            if is_container:
                _scan_zip(
                    content,
                    full + "!/",
                    depth + 1,
                    actual,
                    authorized,
                    prepared,
                    sources,
                    dex_classes,
                    signing,
                    profile_secret,
                    extra_secret,
                    probes,
                    totals,
                )
            elif (len(content) >= 262 and content[257:262] == b"ustar") or content.startswith(
                (b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!", b"\x28\xb5\x2f\xfd")
            ):
                fail("UNREVIEWED_ARCHIVE")
        cursor = 0
        for start, end in sorted(ranges):
            if start != cursor or end < start:
                fail("ZIP_UNREVIEWED_GAP")
            cursor = end
        if cursor != boundary:
            fail("ZIP_UNREVIEWED_GAP")


def _read_trusted(trusted, prepared, delivery):
    if not isinstance(trusted, tuple) or len(trusted) != 2:
        fail("TRUSTED_MANIFEST_REQUIRED")
    path, expected = trusted
    if (
        not isinstance(path, Path)
        or not isinstance(expected, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected)
    ):
        fail("TRUSTED_MANIFEST_REQUIRED")
    _no_links(path)
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        fail("TRUSTED_MANIFEST_INVALID")
    if path.resolve().is_relative_to(prepared.resolve()) or path.resolve().is_relative_to(
        delivery.resolve()
    ):
        fail("TRUSTED_MANIFEST_INVALID")
    raw = path.read_bytes()
    if digest(raw) != expected:
        fail("TRUSTED_MANIFEST_INVALID")
    try:
        manifest = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        fail("TRUSTED_MANIFEST_INVALID")
    required = {
        "version",
        "authorized_pages",
        "source_hashes",
        "prepared",
        "delivery",
        "inventory",
        "apk_sha256",
        "apk_size",
        "signing",
        "certificate_sha256",
        "android_metadata",
        "tools",
        "dex_classes",
        "dex_rebuilt",
    }
    if not isinstance(manifest, dict) or set(manifest) != required or manifest["version"] != 1:
        fail("TRUSTED_MANIFEST_INVALID")
    return manifest


def _check_tools(tools, expected):
    if not isinstance(expected, dict) or set(expected) != set(tools):
        fail("TOOLS_INVALID")
    identities = {}
    for name, path in tools.items():
        if not isinstance(path, Path):
            fail("TOOLS_INVALID")
        _no_links(path)
        if not path.is_file():
            fail("TOOLS_INVALID")
        before = path.stat()
        if before.st_size > MAX_APK_BYTES:
            fail("TOOLS_INVALID")
        raw = path.read_bytes()
        after = path.stat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (
            identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or digest(raw) != expected[name]
        ):
            fail("TOOLS_INVALID")
        identities[name] = identity
    return identities


def _review_public_resources(
    prepared, reviewed_resources, *, profile_secret=(), extra_secret=(), probes=()
):
    _scan_zip(
        (prepared / "assets/public-resources.zip").read_bytes(),
        "assets/public-resources.zip!/",
        1,
        {},
        set(),
        prepared,
        {},
        {},
        {},
        list(profile_secret),
        list(extra_secret),
        probes,
        {"size": 0, "count": 0},
    )
    raw = (prepared / "assets/public-manifest.json").read_bytes()
    try:
        manifest = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        fail("PUBLIC_MANIFEST_INVALID")
    if not isinstance(manifest, dict) or manifest != reviewed_resources:
        fail("PUBLIC_MANIFEST_INVALID")
    try:
        with zipfile.ZipFile(prepared / "assets/public-resources.zip") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(item.is_dir() for item in archive.infolist()):
                fail("PUBLIC_RESOURCE_MISMATCH")
            items = archive.infolist()
            if len(items) > MAX_MEMBERS or sum(item.file_size for item in items) > MAX_TOTAL_BYTES:
                fail("PUBLIC_RESOURCE_MISMATCH")
            content = {item.filename: archive.read(item) for item in items}
            if {name: digest(value) for name, value in content.items()} != manifest:
                fail("PUBLIC_RESOURCE_MISMATCH")
            allowed = {"SEED_IMAGES_MANIFEST.json"}
            for group in ("data", "atlas-cn", "atlas-jp"):
                name = group + "/SOURCE_MANIFEST.json"
                allowed.add(name)
                metadata = json.loads(content[name], object_pairs_hook=unique_object)
                if (
                    not isinstance(metadata, dict)
                    or not isinstance(metadata.get("files"), list)
                    or not metadata["files"]
                ):
                    fail("PUBLIC_RESOURCE_MISMATCH")
                group_names = set()
                for spec in metadata["files"]:
                    if (
                        not isinstance(spec, dict)
                        or not isinstance(spec.get("filename"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", spec["filename"])
                    ):
                        fail("PUBLIC_RESOURCE_MISMATCH")
                    target = group + "/" + spec["filename"]
                    group_names.add(spec["filename"])
                    if (
                        target in allowed
                        or type(spec.get("bytes")) is not int
                        or not 0 < spec["bytes"] <= 16 * 1024 * 1024
                        or spec["bytes"] != len(content[target])
                        or spec.get("sha256") != digest(content[target])
                    ):
                        fail("PUBLIC_RESOURCE_MISMATCH")
                    allowed.add(target)
                if group == "data":
                    singles = {
                        "version.json",
                        "items.json",
                        "entities.json",
                        "mappingPatch.json",
                        "classBoards.json",
                        "grandGraphs.json",
                        "constData.json",
                        "baseSkills.json",
                    }
                    expected_names = set(singles)
                    for part in (
                        "servants",
                        "craftEssences",
                        "wars",
                        "wiki.servants",
                        "mappingData",
                    ):
                        matched = {
                            name
                            for name in group_names
                            if re.fullmatch(re.escape(part) + r"\.[1-9][0-9]*\.json", name)
                        }
                        if not 1 <= len(matched) <= 64 or matched != {
                            f"{part}.{index}.json" for index in range(1, len(matched) + 1)
                        }:
                            fail("PUBLIC_RESOURCE_MISMATCH")
                        expected_names.update(matched)
                    if group_names != expected_names:
                        fail("PUBLIC_RESOURCE_MISMATCH")
                elif group == "atlas-cn" and group_names != {
                    "mstGacha.json",
                    "mstGachaCNExtra.json",
                    "mstShop.json",
                }:
                    fail("PUBLIC_RESOURCE_MISMATCH")
                elif group == "atlas-jp" and group_names != {"mstGacha.json", "mstShop.json"}:
                    fail("PUBLIC_RESOURCE_MISMATCH")
            image = re.compile(
                r"assets/(?:class-[0-9-]+|item-[0-9]+|(?:status|narrow|equip|ce|hero)-[0-9]+|"
                r"banner-(?:jp-)?image-[0-9]+|banner-permanent-fallback)\.png"
            )
            for name in content:
                if image.fullmatch(name):
                    marker = name + ".sha256"
                    stamp = content[marker].decode("ascii")
                    if not re.fullmatch(r"[0-9a-f]{64}:[0-9a-f]{64}", stamp) or stamp[
                        65:
                    ] != digest(content[name]):
                        fail("PUBLIC_RESOURCE_MISMATCH")
                    if not content[name].startswith(b"\x89PNG\r\n\x1a\n"):
                        fail("PUBLIC_RESOURCE_MISMATCH")
                    allowed.update({name, marker})
            native = {name for name in content if name.startswith("native-ui/")}
            contract = ROOT / "src/fgo_stat_core/infrastructure/native_support_ui"
            hashes = json.loads(
                _read_source(contract / "asset_hashes.json"), object_pairs_hook=unique_object
            )
            font = json.loads(
                _read_source(contract / "font_manifest.json"), object_pairs_hook=unique_object
            )
            if not isinstance(hashes, dict) or not hashes or not isinstance(font, dict):
                fail("PUBLIC_RESOURCE_MISMATCH")
            expected_native = {"native-ui/" + name: value for name, value in hashes.items()}
            expected_native["native-ui/" + font["key"]] = font["sha256"]
            if native != set(expected_native) or any(
                digest(content[name]) != value for name, value in expected_native.items()
            ):
                fail("PUBLIC_RESOURCE_MISMATCH")
            if len(content["native-ui/" + font["key"]]) != font["bytes"]:
                fail("PUBLIC_RESOURCE_MISMATCH")
            allowed.update(native)
            for name, expected in FONT_HASHES.items():
                if name not in content or digest(content[name]) != expected:
                    fail("PUBLIC_RESOURCE_MISMATCH")
                allowed.add(name)
            seed = json.loads(content["SEED_IMAGES_MANIFEST.json"], object_pairs_hook=unique_object)
            expected_seed = {
                name.removeprefix("assets/"): digest(raw)
                for name, raw in content.items()
                if name.startswith("assets/")
            }
            if seed != expected_seed or set(content) != allowed:
                fail("PUBLIC_RESOURCE_MISMATCH")
    except (OSError, zipfile.BadZipFile, ValueError, UnicodeError, TypeError, KeyError):
        fail("PUBLIC_RESOURCE_MISMATCH")


def _verify_rebuilt_dex(rebuilt_dir, expected, inventory):
    if not isinstance(rebuilt_dir, Path) or not isinstance(expected, dict) or not expected:
        fail("DEX_PROVENANCE_REQUIRED")
    _no_links(rebuilt_dir)
    if not rebuilt_dir.is_dir():
        fail("DEX_PROVENANCE_REQUIRED")
    actual_dex = {name for name in inventory if name.lower().endswith(".dex")}
    if set(expected) != actual_dex:
        fail("DEX_PROVENANCE_MISMATCH")
    for name, value in expected.items():
        if "/" in name or "\\" in name or not name.lower().endswith(".dex"):
            fail("DEX_PROVENANCE_MISMATCH")
        path = rebuilt_dir / name
        _no_links(path)
        if not path.is_file() or path.stat().st_size > MAX_APK_BYTES:
            fail("DEX_PROVENANCE_MISMATCH")
        if digest(path.read_bytes()) != value or inventory[name]["sha256"] != value:
            fail("DEX_PROVENANCE_MISMATCH")


def review_authorized_android(
    apk_path,
    prepared,
    *,
    trusted,
    secret_values,
    java,
    apksigner_jar,
    aapt,
    dex_rebuilt_dir=None,
    zipalign=None,
    stage_review=None,
    resource_review=None,
):
    if not all(
        isinstance(path, Path) for path in (apk_path, prepared, java, apksigner_jar, aapt, zipalign)
    ):
        fail("AUDIT_INPUT_INVALID")
    _no_links(apk_path)
    delivery = apk_path.parent
    if not all(
        isinstance(item, tuple) and len(item) == 2 for item in (stage_review, resource_review)
    ):
        fail("INDEPENDENT_REVIEW_REQUIRED")
    for path, expected in (stage_review, resource_review):
        if (
            not isinstance(path, Path)
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            fail("INDEPENDENT_REVIEW_REQUIRED")
        _no_links(path)
        if path.resolve().is_relative_to(prepared.resolve()) or path.resolve().is_relative_to(
            delivery.resolve()
        ):
            fail("INDEPENDENT_REVIEW_REQUIRED")
    manifest = _read_trusted(trusted, prepared, delivery)
    if set(manifest["authorized_pages"]) != AUTHORIZED_PAGES or len(
        manifest["authorized_pages"]
    ) != len(AUTHORIZED_PAGES):
        fail("AUTHORIZED_PAGE_SCOPE_INVALID")
    if (
        not isinstance(secret_values, (tuple, list))
        or not secret_values
        or not any(not isinstance(value, SigningAlias) for value in secret_values)
        or any(
            not isinstance(value, bytes)
            or not (1 if isinstance(value, SigningAlias) else 8) <= len(value) <= 4096
            for value in secret_values
        )
    ):
        fail("AUDIT_INPUT_INVALID")
    tools = {"java": java, "apksigner_jar": apksigner_jar, "aapt": aapt, "zipalign": zipalign}
    tool_identities = _check_tools(tools, manifest["tools"])
    profile_path = ROOT / "game-protocol.json"
    _no_links(profile_path)
    if not profile_path.is_file() or not 1 <= profile_path.stat().st_size <= 16384:
        fail("PROFILE_REQUIRED")
    try:
        profile = checked_profile(
            json.loads(profile_path.read_bytes(), object_pairs_hook=unique_object)
        )
    except (ValueError, UnicodeError, ExportError):
        fail("PROFILE_INVALID")
    expected_profile = profile_values(profile)
    profile_secret = [
        expected_profile[name].encode("utf8")
        for name in ("sdk_key", "development_code", "session_prefix")
    ]
    try:
        staged = checked_profile(
            json.loads((prepared / PROFILE_ASSET).read_bytes(), object_pairs_hook=unique_object)
        )
        if profile_values(staged) != expected_profile:
            fail("PROFILE_MISMATCH")
        prepared_identity = _tree(
            prepared, manifest["prepared"], profile_secret, list(secret_values)
        )
        delivery_identity = _tree(
            delivery,
            manifest["delivery"],
            profile_secret,
            list(secret_values),
            approved_apk=apk_path.name,
        )
        if len(manifest["delivery"]) != 1 or set(
            name for name, record in manifest["delivery"].items() if record["kind"] == "file"
        ) != {apk_path.name}:
            fail("DELIVERY_SCOPE_INVALID")
        verify_inventory(prepared, *stage_review, *resource_review)
        data, sources = _verify_prepared(prepared, manifest["source_hashes"])
        resources = read_resource_review(*resource_review, excluded_roots=(prepared, delivery))
        probes = _project_probes()
        _review_public_resources(
            prepared,
            resources,
            profile_secret=profile_secret,
            extra_secret=secret_values,
            probes=probes,
        )
        authorized = {APP_ARCHIVE + "!/" + name for name in AUTHORIZED_PAGES}
        raw = apk_path.read_bytes()
        if len(raw) != manifest["apk_size"] or digest(raw) != manifest["apk_sha256"]:
            fail("APK_HASH_MISMATCH")
        actual = {}
        _scan_zip(
            raw,
            "",
            0,
            actual,
            authorized,
            prepared,
            sources,
            manifest["dex_classes"],
            manifest["signing"],
            profile_secret,
            list(secret_values),
            probes,
            {"size": 0, "count": 0},
        )
        if actual != manifest["inventory"]:
            fail("APK_INVENTORY_MISMATCH")
        expected_assets = {
            "assets/" + path.relative_to(prepared / "assets").as_posix(): digest(path.read_bytes())
            for path in (prepared / "assets").rglob("*")
            if path.is_file()
        }
        actual_assets = {
            name: record["sha256"]
            for name, record in actual.items()
            if name.startswith("assets/")
            and not name.startswith("assets/chaquopy/")
            and "!/" not in name
            and record["kind"] == "file"
        }
        if actual_assets != expected_assets:
            fail("APK_ASSET_SCOPE_MISMATCH")
        if set(manifest["dex_classes"]) != {
            name for name in actual if name.lower().endswith(".dex")
        }:
            fail("DEX_CLASS_MISMATCH")
        app_expected = set(sources) | set(data)
        app_actual = {
            name.removeprefix(APP_ARCHIVE + "!/")
            for name, record in actual.items()
            if name.startswith(APP_ARCHIVE + "!/") and record["kind"] == "file"
        }
        if app_actual != app_expected or PROFILE_ASSET not in actual:
            fail("APK_APP_SCOPE_MISMATCH")
        _verify_rebuilt_dex(dex_rebuilt_dir, manifest["dex_rebuilt"], actual)
        expected_permissions = [
            "android.permission.INTERNET",
            "android.permission.WRITE_EXTERNAL_STORAGE",
        ]
        metadata = manifest["android_metadata"]
        if (
            metadata.get("application_id") != "io.github.ayamedog.fgostats"
            or metadata.get("permissions") != expected_permissions
        ):
            fail("APK_METADATA_MISMATCH")
        if not verify_signature(java, apksigner_jar, apk_path, manifest["certificate_sha256"]):
            fail("APK_SIGNATURE_VERIFY_FAILED")
        if not verify_android_metadata(aapt, apk_path, metadata):
            fail("APK_METADATA_MISMATCH")
        align = subprocess.run(
            [str(zipalign), "-c", "-P", "16", "4", str(apk_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if align.returncode:
            fail("APK_ALIGNMENT_INVALID")
        if _check_tools(tools, manifest["tools"]) != tool_identities:
            fail("TOOLS_CHANGED")
        if (
            _tree(prepared, manifest["prepared"], profile_secret, list(secret_values))
            != prepared_identity
        ):
            fail("STAGE_CHANGED")
        if (
            _tree(
                delivery,
                manifest["delivery"],
                profile_secret,
                list(secret_values),
                approved_apk=apk_path.name,
            )
            != delivery_identity
        ):
            fail("DELIVERY_CHANGED")
        if _project_probes() != probes:
            fail("SOURCE_CHANGED")
        _verify_prepared(prepared, manifest["source_hashes"])
        verify_inventory(prepared, *stage_review, *resource_review)
        return {
            "apk_sha256": manifest["apk_sha256"],
            "apk_bytes": manifest["apk_size"],
            "inventory": actual,
            "scope": "仅本次 Android 授权页面与五字段；独立清单复核通过",
        }
    except (
        AuditError,
        ExportError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
    ) as error:
        code = str(error) if isinstance(error, AuditError) else "AUDIT_FAILED"
        raise RuntimeError(code) from None
    finally:
        profile_secret.clear()
        expected_profile.clear()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authorized-android-pages-for-this-release", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--java", type=Path)
    parser.add_argument("--apksigner-jar", type=Path)
    parser.add_argument("--aapt", type=Path)
    parser.add_argument("--zipalign", type=Path)
    parser.add_argument("--dex-rebuilt-dir", type=Path)
    parser.add_argument("--stage-manifest", type=Path)
    parser.add_argument("--stage-manifest-sha256")
    parser.add_argument("--resource-review", type=Path)
    parser.add_argument("--resource-review-sha256")
    args = parser.parse_args()
    if not args.authorized_android_pages_for_this_release:
        require_source_free_release()
    if not all(
        (
            args.manifest,
            args.manifest_sha256,
            args.java,
            args.apksigner_jar,
            args.aapt,
            args.dex_rebuilt_dir,
            args.zipalign,
            args.stage_manifest,
            args.stage_manifest_sha256,
            args.resource_review,
            args.resource_review_sha256,
        )
    ):
        raise RuntimeError("AUDIT_INPUT_INVALID")
    _no_links(args.output.parent)
    if (
        args.output.exists()
        or args.output.resolve().is_relative_to(args.prepared.resolve())
        or args.output.resolve().is_relative_to(args.apk.parent.resolve())
    ):
        raise RuntimeError("AUDIT_OUTPUT_INVALID")
    # 仅通过标准输入接收额外的秘密排除值，不记录、展示或写入审阅报告。
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("AUDIT_INPUT_INVALID")
    try:
        values = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise RuntimeError("AUDIT_INPUT_INVALID") from None
    if isinstance(values, list):
        passwords, aliases = values, []
    elif isinstance(values, dict) and set(values) == {"secret_values", "signing_aliases"}:
        passwords, aliases = values["secret_values"], values["signing_aliases"]
    else:
        raise RuntimeError("AUDIT_INPUT_INVALID")
    if not isinstance(passwords, list) or not isinstance(aliases, list) or any(
        not isinstance(value, str) for value in passwords + aliases
    ):
        raise RuntimeError("AUDIT_INPUT_INVALID")
    forbidden = [value.encode("utf8") for value in passwords] + [
        SigningAlias(value.encode("utf8")) for value in aliases
    ]
    raw = values = passwords = aliases = None
    result = review(
        args.apk,
        args.prepared,
        approved_pages=True,
        trusted=(args.manifest, args.manifest_sha256),
        secret_values=forbidden,
        java=args.java,
        apksigner_jar=args.apksigner_jar,
        aapt=args.aapt,
        dex_rebuilt_dir=args.dex_rebuilt_dir,
        zipalign=args.zipalign,
        stage_review=(args.stage_manifest, args.stage_manifest_sha256),
        resource_review=(args.resource_review, args.resource_review_sha256),
    )
    forbidden.clear()
    with args.output.open("x", encoding="utf8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print("APK_REVIEW_PASSED")


if __name__ == "__main__":
    try:
        main()
    except (AuditError, RuntimeError, OSError, ValueError, TypeError, KeyError) as error:
        value = str(error)
        print(value if re.fullmatch(r"[A-Z_]{3,64}", value) else "APK_REVIEW_BLOCKED")
        raise SystemExit(2) from None
