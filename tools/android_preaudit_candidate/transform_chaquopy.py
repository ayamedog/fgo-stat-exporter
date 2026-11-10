"""Candidate for pre-compression Chaquopy transformation, no approval command."""

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from pyc_strip_candidate import rewrite_build_json, rewrite_imy


def unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[name] = value
    return result


def _regular(root, name):
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or name.startswith("/")
        or any(x in ("", ".", "..") for x in name.split("/"))
    ):
        raise ValueError("INVALID_ASSET_PATH")
    path = root.joinpath(*PurePosixPath(name).parts)
    for child in (path, *path.parents):
        if child.is_symlink() or getattr(child.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("ASSET_LINK_FORBIDDEN")
    if not path.is_file():
        raise ValueError("ASSET_NOT_FILE")
    return path


def transform(root, approved, expected_sha256):
    """Transform exact independently reviewed assets and update build.json.

    approved is outside the input tree. Each IMY lists every original member,
    its digest and a strip_pyc/copy/drop action. No automatic review is offered.
    """
    root, approved = Path(root).absolute(), Path(approved).absolute()
    if approved == root or root in approved.parents:
        raise ValueError("APPROVAL_IN_INPUT")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("APPROVAL_INVALID")
    raw = approved.read_bytes()
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("APPROVAL_INVALID")
    spec = json.loads(raw, object_pairs_hook=unique)
    if (
        not isinstance(spec, dict)
        or spec.get("version") not in (1, 2)
        or not isinstance(spec.get("assets"), dict)
    ):
        raise ValueError("APPROVAL_INVALID")
    version = spec["version"]
    expected_keys = (
        {"version", "build_json_sha256", "assets"}
        if version == 1
        else {"version", "normalized_build_sha256", "assets"}
    )
    if set(spec) != expected_keys:
        raise ValueError("APPROVAL_INVALID")
    build = _regular(root, "build.json").read_bytes()
    if version == 1 and hashlib.sha256(build).hexdigest() != spec["build_json_sha256"]:
        raise ValueError("BUILD_JSON_HASH_MISMATCH")
    original, replacements = {}, {}
    total_size = 0
    for name, entry in spec["assets"].items():
        allowed = (
            ({"sha256"}, {"sha256", "members"})
            if version == 1
            else ({"sha256"}, {"normalized_sha256", "members"})
        )
        if not isinstance(entry, dict) or set(entry) not in allowed:
            raise ValueError("APPROVAL_INVALID")
        asset = _regular(root, name)
        size = asset.stat().st_size
        total_size += size
        if size > 256 * 1024 * 1024 or total_size > 512 * 1024 * 1024:
            raise ValueError("ASSET_SIZE_LIMIT")
        content = asset.read_bytes()
        if "sha256" in entry and hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("ASSET_HASH_MISMATCH")
        original[name] = content
        if name.endswith(".imy"):
            if "members" not in entry:
                raise ValueError("MEMBERS_MISSING")
            replacements[name] = rewrite_imy(
                content, entry["members"], allow_frontend=name == "app.imy"
            )
            if (
                version == 2
                and hashlib.sha256(replacements[name]).hexdigest() != entry["normalized_sha256"]
            ):
                raise ValueError("NORMALIZED_ASSET_HASH_MISMATCH")
        elif "members" in entry:
            raise ValueError("MEMBERS_INVALID")
    new_build = rewrite_build_json(build, original, replacements)
    if version == 2 and hashlib.sha256(new_build).hexdigest() != spec["normalized_build_sha256"]:
        raise ValueError("NORMALIZED_BUILD_HASH_MISMATCH")
    replacements["build.json"] = new_build
    staged = []
    try:
        for name, content in replacements.items():
            target = _regular(root, name)
            fd, tmp = tempfile.mkstemp(prefix=".reviewed-", dir=target.parent)
            staged.append((Path(tmp), target))
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
        for tmp, target in staged:
            os.replace(tmp, target)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--approved", required=True)
    parser.add_argument("--approved-sha256", required=True)
    args = parser.parse_args()
    transform(args.root, args.approved, args.approved_sha256)
