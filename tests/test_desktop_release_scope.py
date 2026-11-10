"""扫码可用性与完整发布范围的拒绝回归，使用人工数据。"""

import hashlib
import importlib
import shutil
import subprocess
from pathlib import Path

import pytest


def test_desktop_release_scope_includes_isolated_captcha_assets(monkeypatch):
    root = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(root / "tools"))
    runtime = importlib.import_module("release_parts.runtime")
    captcha = {"ui/captcha-frame.html", "ui/captcha-frame.js"}
    assert captcha <= set(runtime.STATIC_FILES)
    for name in captcha:
        assert (root / "src/fgo_local_exporter" / name).is_file()


def test_scan_consent_and_missing_configuration_controls():
    node = shutil.which("node")
    if node is None:
        pytest.skip("前端状态回归需要 Node.js")
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [node, str(root / "tests/desktop_controls.cjs"),
         str(root / "src/fgo_local_exporter/ui/app.js")],
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_release_review_rejects_unlisted_files_even_with_valid_payload_hashes(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    reviewer = importlib.import_module("review_desktop")
    (tmp_path / "runtime.txt").write_bytes(b"synthetic-runtime")
    expected = {"runtime.txt": hashlib.sha256(b"synthetic-runtime").hexdigest()}
    reviewer.tree(tmp_path, expected)
    (tmp_path / "unexpected-source.py").write_text("# synthetic development file")
    with pytest.raises(RuntimeError, match="完整暂存目录与发布白名单不符"):
        reviewer.tree(tmp_path, expected)


@pytest.mark.parametrize("name", ["../private.json", "tests/fixture.json", "C:/private.json"])
def test_release_review_rejects_unsafe_or_development_paths(name, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    reviewer = importlib.import_module("review_desktop")
    with pytest.raises(RuntimeError):
        reviewer.safe_name(name)
