"""Verify externally reviewed AGP input trees; development-only, no freeze command."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat


class AuditFailure(Exception):
    pass


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise AuditFailure("DUPLICATE_MANIFEST_KEY")
        result[name] = value
    return result


def _no_link(path):
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
        raise AuditFailure("LINK_FORBIDDEN")
    return st


def _hash(path):
    if _no_link(path).st_size > 1024 * 1024 * 1024:
        raise AuditFailure("FILE_SIZE_LIMIT")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _inventory(root):
    root = pathlib.Path(root).absolute()
    # Every ancestor must also be free of Windows junctions and symlinks.
    for part in (root, *root.parents):
        if os.path.lexists(part):
            _no_link(part)
    if not os.path.lexists(root):
        return {"kind": "absent"}
    root_stat = _no_link(root)
    if stat.S_ISREG(root_stat.st_mode):
        return {"kind": "file", "sha256": _hash(root), "size": root_stat.st_size}
    if not stat.S_ISDIR(root_stat.st_mode):
        raise AuditFailure("SPECIAL_FORBIDDEN")
    directories, files = [], {}
    for current, names, filenames in os.walk(root, followlinks=False):
        current_path = pathlib.Path(current)
        for name in names + filenames:
            path = current_path / name
            st = _no_link(path)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(st.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(st.st_mode):
                files[relative] = {"size": st.st_size, "sha256": _hash(path)}
            else:
                raise AuditFailure("SPECIAL_FORBIDDEN")
            if len(directories) + len(files) > 100000:
                raise AuditFailure("ENTRY_LIMIT")
    return {"kind": "directory", "directories": sorted(directories), "files": files}


def verify_frozen(inputs, manifest_path, expected_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise AuditFailure("MANIFEST_HASH_INVALID")
    manifest_path = pathlib.Path(manifest_path).absolute()
    for part in (manifest_path, *manifest_path.parents):
        _no_link(part)
    raw = manifest_path.read_bytes()
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AuditFailure("MANIFEST_HASH_INVALID")
    try:
        manifest = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError) as exc:
        raise AuditFailure("MANIFEST_INVALID") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"version", "inputs"}
        or manifest["version"] != 1
        or not isinstance(manifest["inputs"], dict)
        or set(inputs) != set(manifest["inputs"])
    ):
        raise AuditFailure("INPUT_SET_MISMATCH")
    roots = [pathlib.Path(value).absolute() for value in inputs.values()]
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(roots)
        for b in roots[i + 1 :]
    ):
        raise AuditFailure("INPUT_ROOT_OVERLAP")
    if any(manifest_path == path or path in manifest_path.parents for path in roots):
        raise AuditFailure("MANIFEST_IN_INPUT")
    for name, path in inputs.items():
        if _inventory(path) != manifest["inputs"][name]:
            raise AuditFailure("FROZEN_INPUT_MISMATCH")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--input", action="append", default=[])
    args = parser.parse_args()
    values = {}
    for item in args.input:
        name, separator, value = item.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[a-z][a-z0-9_-]*", name)
            or not value
            or name in values
        ):
            raise AuditFailure("INPUT_ARGUMENT_INVALID")
        values[name] = value
    verify_frozen(values, args.manifest, args.manifest_sha256)
