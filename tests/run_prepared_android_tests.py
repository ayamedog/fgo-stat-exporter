"""在真实 Android 暂存 pyc 上运行人工测试；禁止回退到项目源码。"""

import importlib.abc
import importlib.machinery
import os
import socket
import sys
from pathlib import Path

import pytest

if len(sys.argv) != 2:
    raise SystemExit("需要 Android 暂存目录")

stage = Path(sys.argv[1]).resolve(strict=True)
packaged = stage / "python"
if not packaged.is_dir() or any(packaged.rglob("*.py")):
    raise SystemExit("Android 暂存包含源码或缺少编译模块")
project = Path(__file__).resolve().parents[1]
project_src = project / "src"
sys.path[:] = [path for path in sys.path
               if path and Path(path).resolve() not in {project, project_src}]
sys.path.insert(0, str(packaged))
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"


class StageOnlyFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in {"fgo_local_exporter", "fgo_stat_core"} and not fullname.startswith(
            ("fgo_local_exporter.", "fgo_stat_core.")
        ):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if (spec is None or spec.origin is None
                or not Path(spec.origin).resolve().is_relative_to(packaged)):
            raise ImportError("拒绝从 Android 暂存之外导入项目模块")
        return spec


sys.meta_path.insert(0, StageOnlyFinder())


class NoNetworkSocket(socket.socket):
    def connect(self, *_args, **_kwargs):
        raise RuntimeError("人工回归禁止网络访问")

    def connect_ex(self, *_args, **_kwargs):
        raise RuntimeError("人工回归禁止网络访问")

    def sendto(self, *_args, **_kwargs):
        raise RuntimeError("人工回归禁止网络访问")


socket.socket = NoNetworkSocket


class MobileFixturePlugin:
    """通用接口测试改为 Android 实际在进程内导入的装配方式。"""

    def pytest_collection_modifyitems(self, session, config, items):
        import test_local_api

        from fgo_local_exporter.mobile import bounded_import

        original = test_local_api.create_app

        def mobile_app(*args, **kwargs):
            kwargs.setdefault("mobile", True)
            kwargs.setdefault("importer", bounded_import)
            return original(*args, **kwargs)

        test_local_api.create_app = mobile_app


tests = [
    "test_password_local_api.py",
    "test_password_protocol.py",
    "test_qr_switch.py",
    "test_local_api.py",
    "test_report_revision.py",
    "test_mobile.py",
    "test_statistics.py",
    "test_report_model.py",
    "test_report_parameters.py",
]
arguments = ["-q", "-c", os.devnull, "--disable-warnings"]
arguments.extend(str(project / "tests" / name) for name in tests)
result = pytest.main(arguments, plugins=[MobileFixturePlugin()])
for name, module in tuple(sys.modules.items()):
    if name.startswith(("fgo_local_exporter", "fgo_stat_core")):
        origin = getattr(module, "__file__", None)
        if origin is None or not Path(origin).resolve().is_relative_to(packaged):
            raise SystemExit("发现从 Android 暂存之外导入的项目模块")
raise SystemExit(result)
