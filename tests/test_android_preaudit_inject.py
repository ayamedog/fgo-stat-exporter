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
from inject_app_pyc import inject  # noqa: E402


@pytest.fixture
def injection_fixture(tmp_path):
    stage = tmp_path / "stage"
    package = stage / "python" / "pkg"
    package.mkdir(parents=True)
    pyc = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(compile("value=42", "x", "exec"))
    (package / "__init__.pyc").write_bytes(pyc)
    (package / "index.html").write_bytes(b"synthetic html")
    records = {"python/pkg/__init__.pyc": pyc, "python/pkg/index.html": b"synthetic html"}
    manifest = tmp_path / "reviewed.json"
    doc = {
        "version": 1,
        "directories": ["python", "python/pkg"],
        "files": {
            k: {"size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for k, v in records.items()
        },
    }
    manifest.write_text(json.dumps(doc))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    root = tmp_path / "chaquopy"
    root.mkdir()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("pkg/", b"")
        z.writestr("pkg/index.html", b"synthetic html")
    old = out.getvalue()
    (root / "app.imy").write_bytes(old)
    (root / "build.json").write_text(
        json.dumps(
            {
                "assets": {"app.imy": hashlib.sha1(old).hexdigest()},
                "extract_packages": [],
                "python_version": "3.13",
            }
        )
    )
    return root, stage, manifest, digest, pyc


def test_injects_only_reviewed_stage_modules_and_updates_build(injection_fixture):
    root, stage, manifest, digest, pyc = injection_fixture
    inject(root, stage, manifest, digest)
    app = (root / "app.imy").read_bytes()
    assert (
        json.loads((root / "build.json").read_bytes())["assets"]["app.imy"]
        == hashlib.sha1(app).hexdigest()
    )
    with zipfile.ZipFile(io.BytesIO(app)) as z:
        assert set(z.namelist()) == {"pkg/", "pkg/index.html", "pkg/__init__.pyc"}
        assert z.read("pkg/__init__.pyc") == pyc
    with pytest.raises(ValueError, match="APP_MEMBERS_MISMATCH"):
        inject(root, stage, manifest, digest)


@pytest.mark.parametrize("case", ["module", "html", "extra", "missing", "sha1", "manifest"])
def test_injection_rejects_changed_or_extra_inputs(injection_fixture, case):
    root, stage, manifest, digest, _ = injection_fixture
    app = root / "app.imy"
    build = root / "build.json"
    if case == "module":
        (stage / "python/pkg/__init__.pyc").write_bytes(b"changed")
    elif case == "html":
        (stage / "python/pkg/index.html").write_bytes(b"changed")
    elif case in {"extra", "missing"}:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("pkg/", b"")
            if case != "missing":
                archive.writestr("pkg/index.html", b"synthetic html")
                archive.writestr("pkg/unapproved.pyc", b"unexpected")
        app.write_bytes(out.getvalue())
    elif case == "sha1":
        data = json.loads(build.read_bytes())
        data["assets"]["app.imy"] = "0" * 40
        build.write_text(json.dumps(data))
    elif case == "manifest":
        digest = "0" * 64
    before_app, before_build = app.read_bytes(), build.read_bytes()
    with pytest.raises(ValueError):
        inject(root, stage, manifest, digest)
    assert app.read_bytes() == before_app
    assert build.read_bytes() == before_build
