"""使用人工响应检查密码 UI 和隔离验证码消息。"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_password_ui_synthetic_flow():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js 执行客户端页面测试")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/password_ui.cjs"),
         str(root / "src/fgo_local_exporter/ui/app.js")],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert "PASSWORD_UI_SYNTHETIC_OK" in result.stdout
