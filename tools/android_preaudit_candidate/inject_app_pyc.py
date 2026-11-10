"""将独立审核的项目 pyc 装入 Chaquopy app.imy；候选，未接入发布流程。"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from pyc_strip_candidate import rewrite_build_json


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_KEY")
        result[key] = value
    return result


def validate_path(root, relative):
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or relative.startswith("/")
        or any(p in ("", ".", "..") for p in relative.split("/"))
    ):
        raise ValueError("INVALID_PATH")
    path = root.joinpath(*PurePosixPath(relative).parts)
    for item in (path, *path.parents):
        st = item.lstat()
        if item.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ValueError("LINK_FORBIDDEN")
    if not path.is_file():
        raise ValueError("FILE_MISSING")
    return path


def inject(chaquopy_root, stage_root, stage_manifest, stage_manifest_sha256):
    root, stage, manifest = map(
        lambda p: Path(p).absolute(), (chaquopy_root, stage_root, stage_manifest)
    )
    if not re.fullmatch(r"[0-9a-f]{64}", stage_manifest_sha256):
        raise ValueError("MANIFEST_INVALID")
    if (
        manifest == stage
        or stage in manifest.parents
        or manifest == root
        or root in manifest.parents
    ):
        raise ValueError("MANIFEST_IN_INPUT")
    if root == stage or root in stage.parents or stage in root.parents:
        raise ValueError("INPUT_ROOT_OVERLAP")
    for item in (manifest, *manifest.parents):
        st = item.lstat()
        if item.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ValueError("LINK_FORBIDDEN")
    raw = manifest.read_bytes()
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != stage_manifest_sha256:
        raise ValueError("MANIFEST_INVALID")
    doc = json.loads(raw, object_pairs_hook=unique)
    if (
        not isinstance(doc, dict)
        or set(doc) != {"version", "directories", "files"}
        or doc["version"] != 1
        or not isinstance(doc["files"], dict)
        or not isinstance(doc["directories"], list)
    ):
        raise ValueError("MANIFEST_INVALID")
    expected_files = {
        name.removeprefix("python/"): entry
        for name, entry in doc["files"].items()
        if name.startswith("python/")
    }
    expected_dirs = {
        name.removeprefix("python/") + "/"
        for name in doc["directories"]
        if name.startswith("python/")
    }
    pyc = {name for name in expected_files if name.endswith(".pyc")}
    if not pyc or any(name.endswith((".py", ".pyi", ".map")) for name in expected_files):
        raise ValueError("STAGE_SCOPE_INVALID")
    stage_python = stage / "python"
    staged = {}
    total_size = 0
    for name, info in expected_files.items():
        if not isinstance(info, dict) or set(info) != {"size", "sha256"}:
            raise ValueError("MANIFEST_INVALID")
        path = validate_path(stage_python, name)
        size = path.stat().st_size
        total_size += size
        if size > 64 * 1024 * 1024 or total_size > 256 * 1024 * 1024:
            raise ValueError("STAGE_SIZE_LIMIT")
        value = path.read_bytes()
        if len(value) != info["size"] or hashlib.sha256(value).hexdigest() != info["sha256"]:
            raise ValueError("STAGE_HASH_MISMATCH")
        staged[name] = value
    app_path = validate_path(root, "app.imy")
    base = app_path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > 256 * 1024 * 1024:
            raise ValueError("APP_SIZE_LIMIT")
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or set(names) != expected_dirs | (
            set(expected_files) - pyc
        ):
            raise ValueError("APP_MEMBERS_MISMATCH")
        for entry in entries:
            mode = (entry.external_attr >> 16) & 0o170000
            if mode == 0o120000 or entry.flag_bits & 1:
                raise ValueError("APP_MEMBER_INVALID")
            if not entry.is_dir() and archive.read(entry) != staged[entry.filename]:
                raise ValueError("APP_CONTENT_MISMATCH")
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(expected_dirs | set(expected_files)):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o40755 << 16) | 0x10 if name.endswith("/") else 0o100644 << 16
            archive.writestr(info, b"" if name.endswith("/") else staged[name])
    transformed = target.getvalue()
    build_path = validate_path(root, "build.json")
    build_raw = build_path.read_bytes()
    build = json.loads(build_raw, object_pairs_hook=unique)
    if not isinstance(build, dict) or not isinstance(build.get("assets"), dict):
        raise ValueError("BUILD_INVALID")
    originals = {name: validate_path(root, name).read_bytes() for name in build["assets"]}
    updated = rewrite_build_json(build_raw, originals, {"app.imy": transformed})
    temporary = []
    try:
        for path, value in ((app_path, transformed), (build_path, updated)):
            fd, name = tempfile.mkstemp(prefix=".injected-", dir=path.parent)
            temporary.append((Path(name), path))
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
        for source, destination in temporary:
            os.replace(source, destination)
    finally:
        for source, _ in temporary:
            source.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chaquopy-root", required=True)
    parser.add_argument("--stage-root", required=True)
    parser.add_argument("--stage-manifest", required=True)
    parser.add_argument("--stage-manifest-sha256", required=True)
    parser.add_argument("--resource-manifest", required=True)
    parser.add_argument("--resource-manifest-sha256", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from prepare_android import verify_inventory

    verify_inventory(
        args.stage_root,
        args.stage_manifest,
        args.stage_manifest_sha256,
        args.resource_manifest,
        args.resource_manifest_sha256,
    )
    inject(args.chaquopy_root, args.stage_root, args.stage_manifest, args.stage_manifest_sha256)
