"""人工验证源码越界时所有客户端打包和审阅入口均默认拒绝。"""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("script", ["build_desktop.py", "prepare_android.py"])
def test_build_entry_refuses_before_creating_staging(script, tmp_path):
    root = Path(__file__).parents[1]
    target = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(root / "tools" / script), "--output", str(target)],
        capture_output=True,
        timeout=30,
        cwd=root,
    )
    assert result.returncode != 0
    assert "客户端发布已暂停" in result.stderr.decode("utf-8", errors="replace")
    assert not target.exists()


def test_direct_builder_and_reviewers_do_not_allow_release(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    runtime = importlib.import_module("release_parts.runtime")
    desktop = importlib.import_module("review_desktop")
    android = importlib.import_module("review_android")
    with pytest.raises(RuntimeError, match="客户端发布已暂停"):
        runtime.build_runtime(tmp_path, tmp_path, tmp_path, tmp_path, profile=None)
    with pytest.raises(RuntimeError, match="客户端发布已暂停"):
        desktop.review(tmp_path, tmp_path / "synthetic.exe")
    with pytest.raises(RuntimeError, match="客户端发布已暂停"):
        android.review(tmp_path / "synthetic.apk", tmp_path)
