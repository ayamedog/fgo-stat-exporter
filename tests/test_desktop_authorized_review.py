"""回归检查桌面发布审核对源码、测试和异常编译载荷的拒绝。"""

import importlib
import marshal
from pathlib import Path

import pytest


@pytest.fixture
def reviewer(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    return importlib.import_module("review_desktop_authorized")


@pytest.mark.parametrize(
    "path",
    ["../private", "folder/source.py", "folder/code.js.map", "folder/debug.pdb", "tests/a"],
)
def test_rejects_disallowed_release_paths(reviewer, path):
    with pytest.raises(RuntimeError):
        reviewer.name_ok(path)


def test_rejects_invalid_bytecode_and_allows_ordinary_python_metadata(reviewer):
    source = compile("value = 1", "ordinary-module.py", "exec")
    raw = marshal.dumps(source)
    assert reviewer.read_code(raw).co_filename == "ordinary-module.py"
    with pytest.raises(RuntimeError):
        reviewer.read_code(raw + b"unexpected")
    with pytest.raises(RuntimeError):
        reviewer.read_code(b"invalid bytecode")
