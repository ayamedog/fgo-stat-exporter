"""Create a review candidate inventory; this does not approve or freeze inputs."""

import argparse
import hashlib
import json
import os
import pathlib
import re

from frozen_inputs import AuditFailure, _inventory


def propose(inputs, output):
    output = pathlib.Path(output).absolute()
    if not output.parent.is_dir():
        raise AuditFailure("OUTPUT_PARENT_MISSING")
    for part in (output, *output.parents):
        if os.path.lexists(part):
            st = part.lstat()
            if pathlib.Path(part).is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                raise AuditFailure("LINK_FORBIDDEN")
    if output.exists():
        raise AuditFailure("OUTPUT_ALREADY_EXISTS")

    roots = [pathlib.Path(value).absolute() for value in inputs.values()]
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(roots)
        for b in roots[i + 1 :]
    ):
        raise AuditFailure("INPUT_ROOT_OVERLAP")
    if any(output == root or root in output.parents for root in roots):
        raise AuditFailure("MANIFEST_IN_INPUT")

    manifest = {
        "version": 1,
        "inputs": {name: _inventory(path) for name, path in sorted(inputs.items())},
    }
    serialized = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    raw = serialized.encode("utf-8")
    with output.open("xb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Write a review candidate only; no input is approved by this command."
    )
    parser.add_argument("--output", required=True)
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
    if not values:
        raise AuditFailure("INPUT_ARGUMENT_INVALID")
    print("CANDIDATE_MANIFEST_SHA256=" + propose(values, args.output))
