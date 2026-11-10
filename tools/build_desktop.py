"""构建本机桌面包；完成运行验收及归档复核前不作为正式交付物。"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from release_parts.release_guard import require_source_free_release  # noqa: E402

if __name__ == "__main__":
    require_source_free_release()

from release_parts.resources import copy_resources, prepared_resources  # noqa: E402
from release_parts.runtime import (  # noqa: E402
    build_runtime,
    collect_licenses,
    desktop_build_profile,
)
from review_desktop import review  # noqa: E402

from fgo_local_exporter import __version__  # noqa: E402


def main():
    require_source_free_release()
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", type=Path)
    parser.add_argument("--protocol", type=Path, help="指定应用级固定参数，默认使用根目录配置")
    parser.add_argument("--output", type=Path, default=ROOT / f"dist/桌面候选_{__version__}")
    args = parser.parse_args()
    profile = desktop_build_profile(ROOT, args.protocol)
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("输出目录已存在，请使用新的目录，避免覆盖验收记录")
    output.mkdir(parents=True)
    source = prepared_resources(args.resources)
    resources = output / "reviewed-resources"
    resource_hashes = copy_resources(source, resources, ROOT / "src/fgo_stat_core")
    documents = output / "reviewed-documents"
    documents.mkdir()
    document_hashes = {}
    for source_name, target_name in (
        ("LICENSE", "LICENSE.txt"),
        ("DESKTOP.md", "使用说明.txt"),
        ("THIRD_PARTY_NOTICES.md", "第三方说明.txt"),
    ):
        raw = (ROOT / source_name).read_bytes()
        (documents / target_name).write_bytes(raw)
        document_hashes[target_name] = hashlib.sha256(raw).hexdigest()
    licenses, license_hashes = collect_licenses(ROOT, documents / "licenses")
    document_hashes.update({"licenses/" + name: value for name, value in license_hashes.items()})
    actual_documents = {
        path.relative_to(documents).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in documents.rglob("*")
        if path.is_file()
    }
    if actual_documents != document_hashes:
        raise RuntimeError("内置说明与许可超出白名单")
    executable, source_hashes = build_runtime(
        ROOT, output, resources, documents, profile=profile
    )
    candidate = output / "candidate"
    candidate.mkdir()
    shutil.copyfile(executable, candidate / executable.name)
    if {path.name for path in candidate.iterdir()} != {executable.name}:
        raise RuntimeError("单文件交付目录存在额外文件")
    (output / "build-audit.json").write_text(
        json.dumps(
            {
                "source_files": source_hashes,
                "resources": resource_hashes,
                "documents": document_hashes,
                "packages": licenses,
                "executable": executable.name,
                "protocol_sha256": hashlib.sha256(
                    (output / "reviewed-runtime-config/game-protocol.json").read_bytes()
                ).hexdigest(),
                "scope": "本机候选包，尚待运行验收和最终归档检查",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    result = review(output, candidate / executable.name)
    (output / "payload-review.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print("候选程序已生成：" + str(candidate / executable.name))


if __name__ == "__main__":
    main()
