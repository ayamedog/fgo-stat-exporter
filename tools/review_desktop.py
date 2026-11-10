"""重新打开单文件 EXE，核对完整载荷、嵌套归档及扫码参数白名单。"""

import argparse
import ast
import hashlib
import io
import json
import marshal
import re
import sys
import types
import zipfile
from pathlib import Path, PurePosixPath

from PyInstaller.archive.readers import CArchiveReader
from release_parts.release_guard import require_source_free_release
from release_parts.runtime import APP_MODULES, CORE_DATA, CORE_MODULES, STATIC_FILES

from fgo_local_exporter.game_profile import checked_profile

ROOT = Path(__file__).resolve().parents[1]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_name(name, *, framework_module=False):
    name = name.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or ":" in name or any(p in {"..", ".git"} for p in path.parts):
        raise RuntimeError("发布路径不合法")
    # Jinja2 模板运算符与 Werkzeug 包初始化必需的请求构造器均为框架运行代码。
    framework_runtime = framework_module and name in {"jinja2/tests", "werkzeug/test"}
    if not framework_runtime and any(
        p.lower() in {"test", "tests", "__tests__", "fixtures"} for p in path.parts
    ):
        raise RuntimeError("发布载荷包含测试材料")
    return name


def tree(root, expected):
    actual = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("发布暂存目录包含链接")
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    if actual != expected:
        raise RuntimeError("完整暂存目录与发布白名单不符")


def normalized(code):
    return code.replace(
        co_filename="",
        co_consts=tuple(
            normalized(v) if isinstance(v, types.CodeType) else v for v in code.co_consts
        ),
    )


def check_code(actual, source):
    if normalized(actual) != normalized(compile(source.read_bytes(), "review", "exec")):
        raise RuntimeError("发布编译模块与审核源码不符")


def review(build, executable):
    require_source_free_release()
    return _review(build, executable)


def review_authorized_0415(build, executable):
    """按本轮授权审阅 0.4.15 候选，通用发布入口仍保持阻断。"""
    from fgo_local_exporter import __version__

    if __version__ != "0.4.15":
        raise RuntimeError("桌面页面源码授权仅适用于 0.4.15")
    return _review(build, executable)


def _review(build, executable):
    build = build.resolve(strict=True)
    audit = json.loads((build / "build-audit.json").read_bytes())
    sources = ["run_desktop.py"]
    sources += ["src/fgo_local_exporter/" + n + ".py" for n in APP_MODULES]
    sources += ["src/fgo_local_exporter/" + n for n in STATIC_FILES]
    sources += ["src/fgo_stat_core/" + n for n in (*CORE_MODULES, *CORE_DATA)]
    expected_sources = {n: digest((ROOT / n).read_bytes()) for n in sources}
    if audit["source_files"] != expected_sources:
        raise RuntimeError("构建源码集合与当前明确白名单不符")
    tree(build / "source", expected_sources)
    tree(build / "reviewed-resources", audit["resources"])
    tree(build / "reviewed-documents", audit["documents"])
    tree(build / "reviewed-runtime-config", {"game-protocol.json": audit["protocol_sha256"]})
    profile_raw = (build / "reviewed-runtime-config/game-protocol.json").read_bytes()
    profile = checked_profile(json.loads(profile_raw))
    own = {"report-resources/" + n: h for n, h in audit["resources"].items()}
    own.update({"documents/" + n: h for n, h in audit["documents"].items()})
    own.update(
        {n.removeprefix("src/"): expected_sources[n] for n in sources if not n.endswith(".py")}
    )
    own["runtime-config/game-protocol.json"] = digest(profile_raw)
    own["engine-manifest.json"] = digest((build / "engine-manifest.json").read_bytes())
    package = build / "pyinstaller/FGOStatDesktop"
    entries = ast.literal_eval((package / "PKG-00.toc").read_text("utf-8"))[2]
    site = (Path(sys.prefix) / "Lib/site-packages").resolve(strict=True)
    python = Path(sys.base_prefix).resolve()
    system = Path("C:/Windows/System32").resolve()
    archive = CArchiveReader(str(executable))
    expected = {}
    scripts = {
        "run_desktop",
        "pyiboot01_bootstrap",
        "pyi_rth_inspect",
        "pyi_rth_pkgutil",
        "pyi_rth_multiprocessing",
    }
    bootstrap = {
        "struct",
        "pyimod01_archive",
        "pyimod02_importers",
        "pyimod03_ctypes",
        "pyimod04_pywin32",
    }
    for raw_name, raw_source, kind in entries:
        if kind == "OPTION":
            if raw_name != "pyi-contents-directory _internal":
                raise RuntimeError("未知启动选项")
            continue
        name = safe_name(raw_name)
        if kind == "PYZ" and name == "PYZ-00.pyz":
            name = "PYZ.pyz"
        if name in expected:
            raise RuntimeError("发布载荷包含重复路径")
        source = Path(raw_source).resolve(strict=True)
        if kind == "DATA":
            if name in own:
                if digest(source.read_bytes()) != own[name]:
                    raise RuntimeError("应用运行资源与白名单不符")
            elif name != "base_library.zip":
                relative = source.relative_to(site).as_posix()
                metadata = re.fullmatch(
                    r"(?:click|werkzeug|flask|itsdangerous|markupsafe|pydantic)-[0-9.]+\.dist-info/"
                    r"(?:RECORD|WHEEL|METADATA|INSTALLER|REQUESTED|entry_points.txt|top_level.txt|LICENSE.txt|licenses/LICENSE(?:.txt)?)",
                    name,
                )
                runtime_data = name.startswith("tzdata/zoneinfo/") or name in {
                    "tzdata/zones",
                    "certifi/cacert.pem",
                    "certifi/py.typed",
                    "pythonnet/runtime/Python.Runtime.xml",
                    "pythonnet/runtime/Python.Runtime.deps.json",
                    "webview/js/lib/polyfill.js",
                    "webview/js/lib/dom_json.js",
                    "webview/js/state.js",
                    "webview/js/finish.js",
                    "webview/js/api.js",
                    "webview/js/customize.js",
                }
                if relative != name or not (metadata or runtime_data):
                    raise RuntimeError("存在未审核的依赖运行资源")
        elif kind in {"BINARY", "EXTENSION"}:
            if source.suffix.lower() not in {".dll", ".pyd"} or not any(
                source.is_relative_to(root) for root in (site, python, system)
            ):
                raise RuntimeError("存在未审核的二进制依赖")
        elif kind == "PYSOURCE":
            if name not in scripts:
                raise RuntimeError("存在未审核的启动脚本")
        elif kind == "PYMODULE":
            if name not in bootstrap:
                raise RuntimeError("存在未审核的启动模块")
        elif kind != "PYZ" or name != "PYZ.pyz":
            raise RuntimeError("存在未知归档类型")
        expected[name] = (raw_name, source, kind)
    actual = {safe_name(n): n for n in archive.toc}
    if (
        set(actual) != set(expected)
        or len(actual) != len(archive.toc)
        or not set(own) <= set(actual)
    ):
        raise RuntimeError("EXE 完整路径集合与审核白名单不符")
    payload = {}
    for name, (_raw_name, source, kind) in expected.items():
        raw = archive.extract(actual[name])
        if kind in {"DATA", "BINARY", "EXTENSION", "PYZ"} and raw != source.read_bytes():
            raise RuntimeError("EXE 载荷与审核输入不符")
        if kind == "PYSOURCE":
            check_code(marshal.loads(raw), source)
        if kind == "PYMODULE":
            if normalized(marshal.loads(raw)) != normalized(
                marshal.loads(source.read_bytes()[16:])
            ):
                raise RuntimeError("启动编译模块不符")
        if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", raw):
            raise RuntimeError("发布载荷包含私人密钥")
        if name != "runtime-config/game-protocol.json" and any(
            value.encode() in raw
            for value in (
                profile.sdk_key,
                profile.development_code,
                profile.session_prefix,
            )
        ):
            raise RuntimeError("应用固定参数出现在未授权的载荷位置")
        payload[name] = digest(raw)
    pure = ast.literal_eval((package / "PYZ-00.toc").read_text("utf-8"))[1]
    pyz = archive.open_embedded_archive(actual["PYZ.pyz"])
    if set(pyz.toc) != {n for n, _p, _k in pure}:
        raise RuntimeError("嵌套模块集合不符")
    required = {"werkzeug.test", "jinja2.tests", "webview.platforms.edgechromium"}
    for name in sources:
        if name.startswith("src/") and name.endswith(".py"):
            module = name.removeprefix("src/")[:-3].replace("/", ".")
            required.add(module.removesuffix(".__init__"))
    if not required <= set(pyz.toc):
        raise RuntimeError("缺少应用或框架运行必需的编译模块")
    app_modules = 0
    for name, raw_source, _kind in pure:
        safe_name(name.replace(".", "/"), framework_module=True)
        if raw_source == "-":
            namespace = site.joinpath(*name.split("."))
            if (
                not namespace.is_dir() or (namespace / "__init__.py").exists()
                or pyz.toc[name][0] != 3 or pyz.toc[name][2] != 0
                or pyz.extract(name) is not None
            ):
                raise RuntimeError("命名空间包包含未审核内容")
            continue
        source = Path(raw_source).resolve(strict=True)
        if not any(source.is_relative_to(root) for root in (site, python, build / "source/src")):
            raise RuntimeError("模块来自未审核的目录")
        check_code(pyz.extract(name), source)
        if name.startswith(("fgo_local_exporter", "fgo_stat_core")):
            relative = source.relative_to(build / "source").as_posix()
            if relative not in expected_sources:
                raise RuntimeError("额外应用编译模块")
            app_modules += 1
    with zipfile.ZipFile(io.BytesIO(archive.extract(actual["base_library.zip"]))) as nested:
        names = nested.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("标准库归档包含重复路径")
        for name in names:
            safe_name(name)
            if not name.endswith(".pyc"):
                raise RuntimeError("标准库归档包含额外材料")
            check_code(marshal.loads(nested.read(name)[16:]), python / "Lib" / (name[:-1]))
    return {
        "sha256": digest(Path(executable).read_bytes()),
        "payload": payload,
        "application_modules": app_modules,
        "nested_modules": len(pure),
        "scope": "完整载荷、嵌套模块及应用固定参数白名单检查通过",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    result = review(args.build, args.executable)
    (args.build / "payload-review.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"EXE 完整载荷检查通过：{len(result['payload'])} 项，"
        f"{result['application_modules']} 个应用编译模块"
    )


if __name__ == "__main__":
    main()
