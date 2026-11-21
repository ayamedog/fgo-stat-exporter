"""按白名单暂存安卓材料；默认使用项目根目录的本机固定协议配置。"""

import argparse
import hashlib
import importlib.util
import json
import marshal
import shutil
import sys
import types
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from release_parts.release_guard import require_source_free_release  # noqa: E402

_AUTHORIZED_OPTION = "--authorized-frontend-for-this-release"
if __name__ == "__main__" and _AUTHORIZED_OPTION not in sys.argv:
    require_source_free_release()

from release_parts.resources import copy_resources, prepared_resources  # noqa: E402
from release_parts.runtime import CORE_DATA, CORE_MODULES  # noqa: E402

from fgo_local_exporter.game_profile import load_profile, profile_values  # noqa: E402
from fgo_local_exporter.reports import shared_report_revision  # noqa: E402

MODULES = (
    "__init__",
    "errors",
    "game_profile",
    "protocol",
    "statistics",
    "login_session",
    "local_api",
    "settings",
    "reports",
    "sessions",
    "desktop_reports",
    "desktop_resources",
    "resource_catalog",
    "mobile",
)
DATA = (
    "ui/index.html",
    "ui/style.css",
    "ui/app.js",
    "ui/gacha-records.js",
    "ui/gacha-records.css",
    "ui/html-to-image.js",
    "ui/resource-status.js",
    "ui/captcha-frame.html",
    "ui/captcha-frame.js",
    "desktop_static/setup.html",
    "desktop_static/desktop.css",
    "desktop_static/resources.json",
    "mobile_static/mobile.js",
    "mobile_static/mobile.css",
)
LICENSES = (
    ("LICENSE", "project-MIT.txt"),
    ("licenses/html-to-image-MIT.txt", "html-to-image-MIT.txt"),
    ("licenses/Chaquopy-MIT.txt", "Chaquopy-MIT.txt"),
    ("licenses/Python-3.13.txt", "Python-3.13.txt"),
    ("licenses/OpenSSL-Apache-2.0.txt", "OpenSSL-Apache-2.0.txt"),
    ("licenses/NotoSansCJK-OFL-1.1.txt", "NotoSansCJK-OFL-1.1.txt"),
    ("licenses/DejaVu-Fonts.txt", "DejaVu-Fonts.txt"),
    ("licenses/Font-Notices.txt", "Font-Notices.txt"),
    ("licenses/Android-Dependency-Notices.txt", "Android-Dependency-Notices.txt"),
    ("licenses/Android-Rust-Dependency-Notices.txt", "Android-Rust-Dependency-Notices.txt"),
    ("licenses/MPL-2.0.txt", "MPL-2.0.txt"),
)
LICENSE_FIXED_SHA256 = {
    "licenses/NotoSansCJK-OFL-1.1.txt":
        "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2",
    "licenses/DejaVu-Fonts.txt":
        "7a083b136e64d064794c3419751e5c7dd10d2f64c108fe5ba161eae5e5958a93",
    "licenses/Font-Notices.txt":
        "98391ea4a8f22702de0d88d167425441eae64e464e4d439903d44474b501e08d",
    "licenses/Android-Dependency-Notices.txt":
        "a11241c8a88e9831483c42e0eb5807f9397fc10c4bdfe5552e9685957d8727ed",
    "licenses/Android-Rust-Dependency-Notices.txt":
        "d33ad040b86b3697dafcb3a1397dc18175a8d6e154c8cc1f58f646f1c083d1f2",
    "licenses/MPL-2.0.txt":
        "3f3d9e0024b1921b067d6f7f88deb4a60cbe7a78e76c64e3f1d7fc3b779b9d04",
}


def _strip_debug(code):
    if not isinstance(code, types.CodeType):
        raise TypeError("CODE_INVALID")

    def clean(value):
        if isinstance(value, types.CodeType):
            return _strip_debug(value)
        if isinstance(value, tuple):
            return tuple(clean(item) for item in value)
        if isinstance(value, frozenset):
            return frozenset(clean(item) for item in value)
        return value

    return code.replace(
        co_filename="<release>",
        co_firstlineno=1,
        co_linetable=b"",
        co_consts=tuple(clean(value) for value in code.co_consts),
    )


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def compile_release(source):
    """只写编译模块，不把 Python 源码放入 Android 发布暂存。"""
    code = compile(source, "<release>", "exec", dont_inherit=True)
    return importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(_strip_debug(code))


def stage_inventory(root):
    """扫描整个暂存目录，包括不在已登记清单中的目录和文件。"""
    root = Path(root)
    if not root.is_dir() or any(p.is_symlink() or p.is_junction() for p in (root, *root.parents)):
        raise RuntimeError("安卓暂存目录不合法")
    files, directories = {}, []
    for path in sorted(root.rglob("*")):
        if len(files) + len(directories) >= 5000:
            raise RuntimeError("安卓暂存文件数超过限制")
        name = path.relative_to(root).as_posix()
        if path.is_symlink() or path.is_junction():
            raise RuntimeError("安卓暂存目录包含链接")
        if path.is_dir():
            directories.append(name)
        elif path.is_file():
            before = path.stat()
            limits = {"assets/game-protocol.json": 16384, "assets/report-engine.json": 1024,
                      "assets/public-manifest.json": 8 * 1024 * 1024,
                      "assets/public-resources.zip": 512 * 1024 * 1024}
            if before.st_nlink != 1 or before.st_size > limits.get(name, 32 * 1024 * 1024):
                raise RuntimeError("安卓暂存文件不合法")
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            after = path.stat()
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or size != before.st_size):
                raise RuntimeError("安卓暂存文件在审核期间发生变化")
            files[name] = {"size": size, "sha256": digest.hexdigest()}
        else:
            raise RuntimeError("安卓暂存目录包含特殊文件")
    return {"version": 1, "directories": directories, "files": files}


def _source_files():
    return (["fgo_local_exporter/" + name + ".py" for name in MODULES]
            + ["fgo_stat_core/" + name for name in CORE_MODULES])


def _data_files():
    return (["fgo_local_exporter/" + name for name in DATA]
            + ["fgo_stat_core/" + name for name in CORE_DATA])


def approved_source(name):
    source = ROOT / "src" / name
    ancestors = (source, *(ROOT / p for p in source.relative_to(ROOT).parents), *ROOT.parents)
    if (not source.is_file() or not source.resolve().is_relative_to((ROOT / "src").resolve())
            or any(p.is_symlink() or p.is_junction() for p in ancestors)):
        raise RuntimeError("安卓输入不合法：" + name)
    return source


def approved_license(name):
    source = ROOT / name
    ancestors = (source, *(ROOT / p for p in source.relative_to(ROOT).parents), *ROOT.parents)
    if (not source.is_file() or not source.resolve().is_relative_to(ROOT.resolve())
            or any(p.is_symlink() or p.is_junction() for p in ancestors)):
        raise RuntimeError("安卓许可证输入不合法")
    if (name in LICENSE_FIXED_SHA256
            and hashlib.sha256(source.read_bytes()).hexdigest() != LICENSE_FIXED_SHA256[name]):
        raise RuntimeError("安卓字体许可证已变化")
    return source


def expected_stage_files():
    paths = {"python/" + name + "c" for name in _source_files()}
    paths.update("python/" + name for name in _data_files())
    paths.update({
        "assets/game-protocol.json", "assets/report-engine.json", "assets/public-resources.zip",
        "assets/public-manifest.json",
    })
    paths.update("assets/licenses/" + target for _source, target in LICENSES)
    return paths


def expected_stage_directories():
    result = set()
    for name in expected_stage_files():
        result.update(parent.as_posix() for parent in Path(name).parents
                      if parent != Path("."))
    return result


def checked_report_revision():
    """共享指纹只覆盖同一批将被打入 Android 的模块和资源。"""
    core = ROOT / "src/fgo_stat_core"
    actual = {
        path.relative_to(core).as_posix()
        for path in core.rglob("*")
        if path.is_file() and path.suffix in {".py", ".html", ".json"}
    }
    if actual != set(CORE_MODULES) | set(CORE_DATA):
        raise RuntimeError("共享报告源码与安卓运行白名单不一致")
    return shared_report_revision(ROOT / "src")


def check_public_name(name):
    if (not isinstance(name, str) or not name or "\\" in name
            or name.startswith("/") or ":" in name):
        raise RuntimeError("公开资源路径不合法")
    parts = name.split("/")
    if (any(part in {"", ".", ".."} for part in parts)
            or any(part.casefold() in {"test", "tests", "fixtures", ".git"} for part in parts)
            or not name.endswith((".json", ".png", ".sha256", ".otf", ".ttf", ".ttc"))):
        raise RuntimeError("公开资源包含未授权文件")


def check_required_fonts_and_native_resources(public):
    """只接受经用户确认分发且与共享资源契约一致的原生素材和字体。"""
    contract_root = "fgo_stat_core/infrastructure/native_support_ui/"
    hashes = json.loads(approved_source(contract_root + "asset_hashes.json").read_bytes(),
                        object_pairs_hook=unique_pairs)
    font = json.loads(approved_source(contract_root + "font_manifest.json").read_bytes(),
                      object_pairs_hook=unique_pairs)
    expected = {"native-ui/" + name: digest for name, digest in hashes.items()}
    expected["native-ui/" + font["key"]] = font["sha256"]
    expected.update({
        "fonts/NotoSansCJK-Regular.ttc":
            "d18a3adba5c891eb7f9cdf8c31dad28dcc324db80e662d6a0f91e8db6aee5913",
        "fonts/DejaVuSans.ttf":
            "f9a7bd58cdaa9d1e9acafd070db8dea0b6db9cf2b540501b15075315a47cba3d",
    })
    if any(public.get(name) != digest for name, digest in expected.items()):
        raise RuntimeError("已授权的原生素材或字体缺失或哈希不符")


def propose_resource_review(source, work, manifest_output):
    """只准备可审阅的公开资源及路径/哈希候选，不创建归档。"""
    source = prepared_resources(source)
    work, manifest_output = map(Path, (work, manifest_output))
    for candidate in (source, work, manifest_output):
        if any(p.is_symlink() or p.is_junction() for p in (candidate, *candidate.parents)):
            raise RuntimeError("资源候选路径不能经过链接")
    if not source.is_dir() or work.exists() or manifest_output.exists():
        raise RuntimeError("资源来源不存在或候选目录已存在")
    resolved_source, resolved_work, resolved_manifest = (
        source.resolve(), work.resolve(), manifest_output.resolve()
    )
    if (resolved_work.is_relative_to(resolved_source)
            or resolved_source.is_relative_to(resolved_work)
            or resolved_manifest.is_relative_to(resolved_work)
            or resolved_manifest.is_relative_to(resolved_source)):
        raise RuntimeError("资源来源、工作目录和候选清单必须相互隔离")
    resolved_work.mkdir(parents=True, exist_ok=False)
    public = copy_resources(resolved_source, resolved_work / "reviewed-public-resources",
                            ROOT / "src/fgo_stat_core")
    for name in public:
        check_public_name(name)
    check_required_fonts_and_native_resources(public)
    inventory = stage_inventory(resolved_work)
    expected = {"reviewed-public-resources/" + name for name in public}
    expected_dirs = {"reviewed-public-resources"}
    for name in expected:
        expected_dirs.update(p.as_posix() for p in Path(name).parents if p != Path("."))
    if (set(inventory["files"]) != expected
            or set(inventory["directories"]) != expected_dirs
            or any(inventory["files"]["reviewed-public-resources/" + name]["sha256"] != digest
                   for name, digest in public.items())):
        raise RuntimeError("公开资源候选完整范围不符")
    with resolved_manifest.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(sorted(public.items())), indent=2))


def read_resource_review(review_path, expected_sha256, *, excluded_roots=()):
    review_path = Path(review_path)
    if (any(review_path.resolve().is_relative_to(Path(root).resolve()) for root in excluded_roots)
            or any(p.is_symlink() or p.is_junction() for p in (review_path, *review_path.parents))):
        raise RuntimeError("资源审核清单必须独立且不能经过链接")
    if not review_path.is_file() or review_path.stat().st_size > 8 * 1024 * 1024:
        raise RuntimeError("公开资源审核清单过大或不可读")
    raw = review_path.read_bytes()
    if (len(raw) > 8 * 1024 * 1024 or not isinstance(expected_sha256, str)
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise RuntimeError("公开资源审核清单摘要不符")
    try:
        result = json.loads(raw, object_pairs_hook=unique_pairs)
        if (not isinstance(result, dict) or not result
                or any(not isinstance(v, str) or len(v) != 64
                       or any(c not in "0123456789abcdef" for c in v) for v in result.values())):
            raise ValueError("invalid reviewed resources")
        for name in result:
            check_public_name(name)
        return result
    except (ValueError, UnicodeError):
        raise RuntimeError("公开资源审核清单不合法") from None


def check_static_staged(root):
    """校验实际模块、页面、配置与许可，供归档前后共用。"""
    root = Path(root)
    for name in _source_files():
        if (root / "python" / (name + "c")).read_bytes() != compile_release(
            approved_source(name).read_bytes()
        ):
            raise RuntimeError("安卓编译模块与审核源码不符")
    for name in _data_files():
        if (root / "python" / name).read_bytes() != approved_source(name).read_bytes():
            raise RuntimeError("安卓运行资源与授权文件不符")
    for source, target in LICENSES:
        if ((root / "assets/licenses" / target).read_bytes()
                != approved_license(source).read_bytes()):
            raise RuntimeError("安卓许可证与审核文件不符")
    from fgo_local_exporter.game_profile import checked_profile

    try:
        staged_profile = checked_profile(json.loads(
            (root / "assets/game-protocol.json").read_bytes(), object_pairs_hook=unique_pairs
        ))
        approved_profile = build_profile()
        if (approved_profile is None
                or profile_values(staged_profile) != profile_values(approved_profile)):
            raise RuntimeError("安卓应用参数与批准的固定配置不符")
        engine = json.loads((root / "assets/report-engine.json").read_bytes(),
                            object_pairs_hook=unique_pairs)
        if engine != {"version": 1, "revision": checked_report_revision()}:
            raise RuntimeError("安卓报告引擎指纹与共享源码不符")
    except (ValueError, UnicodeError):
        raise RuntimeError("安卓配置不合法") from None


def verify_inventory(root, manifest_path, expected_sha256, resource_review, resource_sha256):
    """固定独立清单摘要后，再核对暂存的全部实际路径和载荷。"""
    root = Path(root)
    manifest_path = Path(manifest_path)
    if (manifest_path.resolve().is_relative_to(root.resolve())
            or any(p.is_symlink() or p.is_junction() for p in
                   (manifest_path, *manifest_path.parents))):
        raise RuntimeError("安卓清单必须独立且不能经过链接")
    if not manifest_path.is_file() or manifest_path.stat().st_size > 8 * 1024 * 1024:
        raise RuntimeError("安卓独立清单过大或不可读")
    raw = manifest_path.read_bytes()
    if not isinstance(expected_sha256, str) or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("安卓独立清单摘要不符")
    try:
        expected = json.loads(raw, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError):
        raise RuntimeError("安卓独立清单不合法") from None
    inventory = stage_inventory(root)
    if (expected != inventory or set(inventory["files"]) != expected_stage_files()
            or set(inventory["directories"]) != expected_stage_directories()):
        raise RuntimeError("安卓暂存范围或载荷已改变")
    check_static_staged(root)
    try:
        public = json.loads((root / "assets/public-manifest.json").read_bytes(),
                            object_pairs_hook=unique_pairs)
        check_required_fonts_and_native_resources(public)
        if public != read_resource_review(resource_review, resource_sha256, excluded_roots=(root,)):
            raise RuntimeError("公开资源与独立审核清单不符")
        with zipfile.ZipFile(root / "assets/public-resources.zip") as archive:
            entries = archive.infolist()
            if len(entries) != len(public) or len({e.filename for e in entries}) != len(entries):
                raise RuntimeError("安卓公开资源归档范围不符")
            if len(entries) > 5000 or sum(entry.file_size for entry in entries) > 512 * 1024 * 1024:
                raise RuntimeError("安卓公开资源归档超过限制")
            actual = {}
            for entry in entries:
                path = Path(entry.filename)
                if (entry.is_dir() or path.is_absolute() or ".." in path.parts
                        or "\\" in entry.filename or entry.file_size > 64 * 1024 * 1024):
                    raise RuntimeError("安卓公开资源路径不合法")
                data = archive.read(entry)
                if data.startswith(b"PK\x03\x04"):
                    raise RuntimeError("安卓公开资源含未授权嵌套归档")
                actual[entry.filename] = hashlib.sha256(data).hexdigest()
            if actual != public:
                raise RuntimeError("安卓公开资源归档与清单不符")
    except (ValueError, UnicodeError, zipfile.BadZipFile):
        raise RuntimeError("安卓资源或配置不合法") from None


def build_profile(path=None):
    selected = Path(path) if path is not None else ROOT / "game-protocol.json"
    profile = load_profile(selected)
    if path is not None and profile is None:
        raise RuntimeError("指定的游戏固定配置文件不存在")
    return profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(_AUTHORIZED_OPTION, action="store_true")
    parser.add_argument("--propose-resource-review", action="store_true")
    parser.add_argument("--resources", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work", type=Path, help="独立资源构建目录")
    parser.add_argument("--manifest-output", type=Path, help="独立候选清单")
    parser.add_argument("--resource-review", type=Path, help="已审核的公开资源清单")
    parser.add_argument("--resource-review-sha256", help="审核后的公开资源清单摘要")
    parser.add_argument("--verify-manifest", type=Path, help="审核后的独立清单")
    parser.add_argument("--verify-manifest-sha256", help="审核后冻结的独立清单摘要")
    parser.add_argument("--protocol", type=Path, help="指定其他配置，默认读取项目根目录配置")
    args = parser.parse_args()
    if not args.authorized_frontend_for_this_release:
        require_source_free_release()
    if args.propose_resource_review:
        if (not args.work or not args.manifest_output or args.output
                or args.verify_manifest or args.verify_manifest_sha256 or args.resource_review
                or args.resource_review_sha256 or args.protocol):
            raise SystemExit("资源候选参数不完整或与其他模式冲突")
        propose_resource_review(args.resources, args.work, args.manifest_output)
        print("公开资源候选清单已准备，须逐项审核；尚未创建归档。")
        return
    if not args.output:
        raise SystemExit("须指定安卓暂存目录")
    if args.verify_manifest or args.verify_manifest_sha256:
        if (not args.verify_manifest or not args.verify_manifest_sha256
                or args.work or args.manifest_output or not args.resource_review
                or not args.resource_review_sha256):
            raise SystemExit("校验参数不完整")
        verify_inventory(args.output, args.verify_manifest, args.verify_manifest_sha256,
                         args.resource_review, args.resource_review_sha256)
        print("安卓暂存目录与授权范围及冻结清单一致。")
        return
    if (not args.work or not args.manifest_output or not args.resource_review
            or not args.resource_review_sha256):
        raise SystemExit("须指定独立工作目录、候选清单及审核后的资源清单")
    if args.output.exists():
        raise SystemExit("输出目录已存在，请选择新的空目录")
    if args.work.exists() or args.manifest_output.exists():
        raise SystemExit("工作目录或清单已存在")
    for candidate in (args.output, args.work, args.manifest_output):
        if any(p.is_symlink() or p.is_junction() for p in (candidate, *candidate.parents)):
            raise SystemExit("工作目录、暂存和清单不能经过链接")
    output = args.output.resolve()
    work = args.work.resolve()
    manifest_output = args.manifest_output.resolve()
    if (work.is_relative_to(output) or output.is_relative_to(work)
            or manifest_output.is_relative_to(output) or manifest_output.is_relative_to(work)):
        raise SystemExit("工作目录、暂存和清单必须相互隔离")
    reviewed_resources = read_resource_review(args.resource_review, args.resource_review_sha256,
                                               excluded_roots=(output, work))
    check_required_fonts_and_native_resources(reviewed_resources)
    approved_profile = build_profile()
    profile = build_profile(args.protocol) if args.protocol else approved_profile
    if profile is None or approved_profile is None:
        raise SystemExit("缺少应用级固定配置，不能构建正式包")
    if profile_values(profile) != profile_values(approved_profile):
        raise SystemExit("指定配置与批准的固定应用参数不符")
    output.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    source_files = _source_files()
    data_files = _data_files()
    for name in (*source_files, *data_files):
        source = approved_source(name)
        target_name = name + "c" if name in source_files else name
        target = output / "python" / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        target.write_bytes(compile_release(raw) if name in source_files else raw)
    assets = output / "assets"
    assets.mkdir()
    if profile is not None:
        (assets / "game-protocol.json").write_text(
            json.dumps(profile_values(profile), separators=(",", ":")), encoding="utf-8"
        )
    (assets / "report-engine.json").write_text(
        json.dumps({"version": 1, "revision": checked_report_revision()},
                   separators=(",", ":")), encoding="utf-8"
    )
    license_dir = assets / "licenses"
    license_dir.mkdir()
    for source, name in LICENSES:
        shutil.copyfile(approved_license(source), license_dir / name)
    resources = work / "reviewed-public-resources"
    public = copy_resources(
        prepared_resources(args.resources), resources, ROOT / "src/fgo_stat_core"
    )
    if public != reviewed_resources:
        raise RuntimeError("公开资源与独立审核清单不符")
    # 在创建任何归档前，核查工作区与当前安卓暂存的全部路径和载荷。
    work_inventory = stage_inventory(work)
    work_prefix = "reviewed-public-resources/"
    work_files = {work_prefix + name for name in public}
    work_dirs = {"reviewed-public-resources"}
    for name in work_files:
        work_dirs.update(p.as_posix() for p in Path(name).parents if p != Path("."))
    if (set(work_inventory["files"]) != work_files
            or set(work_inventory["directories"]) != work_dirs
            or any(work_inventory["files"][work_prefix + name]["sha256"] != digest
                   for name, digest in public.items())):
        raise RuntimeError("资源工作目录与审核范围不符")
    prepack = stage_inventory(output)
    deferred = {"assets/public-resources.zip", "assets/public-manifest.json"}
    if (set(prepack["files"]) != expected_stage_files() - deferred
            or set(prepack["directories"]) != expected_stage_directories()):
        raise RuntimeError("归档前安卓暂存范围不符")
    check_static_staged(output)
    with zipfile.ZipFile(assets / "public-resources.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(public):
            archive.write(resources / name, name)
    (assets / "public-manifest.json").write_text(json.dumps(public, indent=2))
    with zipfile.ZipFile(assets / "public-resources.zip") as archive:
        reviewed = {
            name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()
        }
        if reviewed != public:
            raise RuntimeError("安卓公开资源归档复核失败")
    inventory = stage_inventory(output)
    if (set(inventory["files"]) != expected_stage_files()
            or set(inventory["directories"]) != expected_stage_directories()):
        raise RuntimeError("安卓完整暂存不符合明确白名单")
    with manifest_output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(inventory, indent=2))
    print("安卓候选暂存已准备，须独立审核并冻结清单后才能构建。", flush=True)


if __name__ == "__main__":
    main()
