import hashlib
import importlib.util
import io
import json
import marshal
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "tools" / "android_preaudit_candidate")
)
from pyc_strip_candidate import rewrite_build_json, rewrite_imy, strip_pyc  # noqa: E402
from transform_chaquopy import transform  # noqa: E402


def test_transform_validates_and_updates_manifest(tmp_path):
    root = tmp_path / "chaquopy"
    root.mkdir()
    data = (
        importlib.util.MAGIC_NUMBER
        + b"\0" * 12
        + marshal.dumps(compile("x=42", "private.py", "exec"))
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("module.pyc", data)
        archive.writestr("test-fixture.py", b"artificial fixture")
    original = buf.getvalue()
    (root / "app.imy").write_bytes(original)
    build = json.dumps(
        {
            "assets": {"app.imy": hashlib.sha1(original).hexdigest()},
            "extract_packages": [],
            "python_version": "3.13",
        }
    ).encode()
    (root / "build.json").write_bytes(build)
    approved = {
        "version": 1,
        "build_json_sha256": hashlib.sha256(build).hexdigest(),
        "assets": {
            "app.imy": {
                "sha256": hashlib.sha256(original).hexdigest(),
                "members": {
                    "module.pyc": {
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "action": "strip_pyc",
                    },
                    "test-fixture.py": {
                        "sha256": hashlib.sha256(b"artificial fixture").hexdigest(),
                        "action": "drop",
                    },
                },
            }
        },
    }
    manifest = tmp_path / "reviewed.json"
    manifest.write_text(json.dumps(approved), encoding="utf8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="APPROVAL_INVALID"):
        transform(root, manifest, "0" * 64)
    assert (root / "app.imy").read_bytes() == original
    transform(root, manifest, digest)
    changed = (root / "app.imy").read_bytes()
    result = json.loads((root / "build.json").read_bytes())
    assert result["assets"]["app.imy"] == hashlib.sha1(changed).hexdigest()
    with zipfile.ZipFile(io.BytesIO(changed)) as archive:
        assert archive.namelist() == ["module.pyc"]
        pyc = archive.read("module.pyc")
        assert pyc[4:16] == b"\0" * 12
        code = marshal.loads(pyc[16:])
        assert code.co_filename == "<release>" and not code.co_linetable


def test_normalized_preapproval_allows_timestamp_only_and_rejects_code_change(tmp_path):
    root = tmp_path / "chaquopy"
    root.mkdir()

    def pack(source, timestamp):
        pyc = importlib.util.MAGIC_NUMBER + b"\0" * 4 + timestamp.to_bytes(4, "little") + b"\0" * 4
        pyc += marshal.dumps(compile(source, "synthetic.py", "exec"))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("module.pyc", pyc)
        return pyc, out.getvalue()

    one, old = pack("x=42", 1)
    two, changed_timestamp = pack("x=42", 2)
    members = {
        "module.pyc": {
            "normalized_sha256": hashlib.sha256(strip_pyc(one)).hexdigest(),
            "action": "strip_pyc",
        }
    }
    normalized = rewrite_imy(old, members)

    def build_for(blob):
        return json.dumps(
            {
                "assets": {"app.imy": hashlib.sha1(blob).hexdigest()},
                "extract_packages": [],
                "python_version": "3.13",
            }
        ).encode()

    expected_build = rewrite_build_json(build_for(old), {"app.imy": old}, {"app.imy": normalized})
    approved = {
        "version": 2,
        "normalized_build_sha256": hashlib.sha256(expected_build).hexdigest(),
        "assets": {
            "app.imy": {
                "normalized_sha256": hashlib.sha256(normalized).hexdigest(),
                "members": members,
            }
        },
    }
    manifest = tmp_path / "reviewed.json"
    manifest.write_text(json.dumps(approved), encoding="utf8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (root / "app.imy").write_bytes(changed_timestamp)
    (root / "build.json").write_bytes(build_for(changed_timestamp))
    transform(root, manifest, digest)
    assert (root / "app.imy").read_bytes() == normalized
    _, altered = pack("x=43", 3)
    (root / "app.imy").write_bytes(altered)
    (root / "build.json").write_bytes(build_for(altered))
    with pytest.raises(ValueError, match="MEMBER_HASH_MISMATCH"):
        transform(root, manifest, digest)
