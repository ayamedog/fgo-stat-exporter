"""Synthetic in-memory-only tests. Never part of the release payload."""

import hashlib
import importlib.util
import io
import marshal
import pathlib
import sys
import zipfile

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "tools" / "android_preaudit_candidate")
)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
from pyc_strip_candidate import rewrite_build_json, rewrite_imy, strip_pyc  # noqa: E402
from review_android import read_stripped_pyc  # noqa: E402


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        for name, data in entries:
            target.writestr(name, data)
    return output.getvalue()


def approval(entries):
    return {
        name: {
            "sha256": hashlib.sha256(data).hexdigest(),
            "action": "strip_pyc" if name.endswith(".pyc") else "copy",
        }
        for name, data in entries
    }


def test_strips_nested_code_and_preserves_execution():
    code = compile(
        "def outer():\n def inner(): return 42\n return inner()\nanswer=outer()\n",
        "C:/private/synthetic.py",
        "exec",
    )
    pyc = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(code)
    entries = [("module.pyc", pyc), ("LICENSE", b"synthetic license")]
    raw = rewrite_imy(archive(entries), approval(entries))
    with zipfile.ZipFile(io.BytesIO(raw)) as result:
        checked = read_stripped_pyc(result.read("module.pyc"))
        namespace = {}
        exec(checked, namespace)
        assert namespace["answer"] == 42
        assert result.read("LICENSE") == b"synthetic license"


@pytest.mark.parametrize(
    "case", ["extra", "missing", "changed", "copy_pyc", "traversal", "duplicate", "source"]
)
def test_rejects_unapproved_or_malformed_members(case):
    data = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(compile("x=1", "x.py", "exec"))
    entries = [("module.pyc", data)]
    reviewed = approval(entries)
    if case == "extra":
        entries.append(("unapproved", b"x"))
    if case == "missing":
        entries.clear()
    if case == "changed":
        entries[0] = ("module.pyc", data + b"x")
    if case == "copy_pyc":
        reviewed["module.pyc"]["action"] = "copy"
    if case == "traversal":
        entries = [("../module.pyc", data)]
        reviewed = approval(entries)
    if case == "duplicate":
        entries.append(entries[0])
    if case == "source":
        entries = [("module.py", b"x=1")]
        reviewed = approval(entries)
    with pytest.raises(ValueError):
        rewrite_imy(archive(entries), reviewed)


def test_rebuilds_chaquopy_inventory_for_rewritten_assets():
    import json

    old = {"app.imy": b"synthetic old", "stdlib-common.imy": b"synthetic unchanged"}
    data = {
        "assets": {name: hashlib.sha1(value).hexdigest() for name, value in old.items()},
        "extract_packages": [],
        "python_version": "3.13",
    }
    raw = json.dumps(data).encode()
    result = json.loads(rewrite_build_json(raw, old, {"app.imy": b"synthetic new"}))
    assert result["assets"]["app.imy"] == hashlib.sha1(b"synthetic new").hexdigest()
    assert result["assets"]["stdlib-common.imy"] == data["assets"]["stdlib-common.imy"]
    with pytest.raises(ValueError):
        rewrite_build_json(raw, {"app.imy": b"wrong"}, {})
    with pytest.raises(ValueError):
        rewrite_build_json(raw, old, {"unknown.imy": b"x"})
    wrong = dict(old, **{"app.imy": b"wrong"})
    with pytest.raises(ValueError):
        rewrite_build_json(raw, wrong, {})


@pytest.mark.parametrize("extension", ["xxlimited", "xxlimited_35", "xxsubtype", "_xxtestfuzz"])
@pytest.mark.parametrize("abi", ["aarch64", "x86_64"])
def test_cpython_example_extensions_cannot_be_copied(extension, abi):
    name = f"{extension}.cpython-313-{abi}-linux-android.so"
    entries = [(name, b"synthetic test extension")]
    reviewed = approval(entries)
    with pytest.raises(ValueError, match="CPYTHON_TEST_EXTENSION_FORBIDDEN"):
        rewrite_imy(archive(entries), reviewed)
    reviewed[name]["action"] = "drop"
    with zipfile.ZipFile(io.BytesIO(rewrite_imy(archive(entries), reviewed))) as result:
        assert result.namelist() == []


def test_drop_pyc_uses_reviewed_normalized_bytes():
    pyc = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(compile("x=1", "x", "exec"))
    spec = {
        "tests/test.pyc": {
            "normalized_sha256": hashlib.sha256(strip_pyc(pyc)).hexdigest(),
            "action": "drop",
        }
    }
    with zipfile.ZipFile(
        io.BytesIO(rewrite_imy(archive([("tests/test.pyc", pyc)]), spec))
    ) as result:
        assert result.namelist() == []
    changed = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(compile("x=2", "x", "exec"))
    with pytest.raises(ValueError, match="MEMBER_HASH_MISMATCH"):
        rewrite_imy(archive([("tests/test.pyc", changed)]), spec)


@pytest.mark.parametrize(
    "name", ["anyio/pytest_plugin.pyc", "flask/testing.pyc", "click/testing.pyc"]
)
def test_test_support_module_is_forbidden_even_if_approved(name):
    pyc = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(compile("x=1", "x", "exec"))
    entries = [(name, pyc)]
    spec = approval(entries)
    with pytest.raises(ValueError, match="TEST_SUPPORT_MODULE_FORBIDDEN"):
        rewrite_imy(archive(entries), spec)
    spec[name]["action"] = "drop"
    with zipfile.ZipFile(io.BytesIO(rewrite_imy(archive(entries), spec))) as result:
        assert result.namelist() == []


@pytest.mark.parametrize("name", ["doctest.pyc", "unittest/case.pyc", "_pyrepl/reader.pyc"])
def test_stdlib_test_and_repl_modules_are_forbidden_even_if_approved(name):
    pyc = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(compile("x=1", "x", "exec"))
    entries = [(name, pyc)]
    spec = approval(entries)
    with pytest.raises(ValueError, match="TEST_OR_REPL_MODULE_FORBIDDEN"):
        rewrite_imy(archive(entries), spec)
    spec[name]["action"] = "drop"
    with zipfile.ZipFile(io.BytesIO(rewrite_imy(archive(entries), spec))) as result:
        assert result.namelist() == []


@pytest.mark.parametrize(
    "name", ["werkzeug/debug/shared/debugger.js", "werkzeug/debug/shared/console.png"]
)
def test_debug_resources_are_forbidden_even_if_approved(name):
    entries = [(name, b"synthetic")]
    spec = approval(entries)
    with pytest.raises(ValueError, match="DEBUG_RESOURCE_FORBIDDEN"):
        rewrite_imy(archive(entries), spec, allow_frontend=True)
    spec[name]["action"] = "drop"
    with zipfile.ZipFile(io.BytesIO(rewrite_imy(archive(entries), spec))) as result:
        assert result.namelist() == []


def test_frontend_must_be_exact_authorized_page_in_app_archive():
    page = "fgo_local_exporter/ui/app.js"
    entries = [(page, b"synthetic")]
    spec = approval(entries)
    with pytest.raises(ValueError, match="UNAUTHORIZED_FRONTEND"):
        rewrite_imy(archive(entries), spec)
    rewrite_imy(archive(entries), spec, allow_frontend=True)
    entries = [("other/app.js", b"synthetic")]
    with pytest.raises(ValueError, match="UNAUTHORIZED_FRONTEND"):
        rewrite_imy(archive(entries), approval(entries), allow_frontend=True)
