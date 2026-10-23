"""客户端公开字典准备；不读取账号、不连接统计网站、不下载可执行代码。"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.resource_catalog import (
    PublicResourceClient,
    discover_catalog,
    validate_url,
)

__all__ = ["ResourceManager", "local_data_root"]

_MANIFEST = Path(__file__).with_name("desktop_static") / "resources.json"


def local_data_root():
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share")))
    if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
        raise ExportError("LOCAL_DIRECTORY_INVALID")
    root = root / "FGOStatExporter"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


class ResourceManager:
    def __init__(self, root=None):
        self.root = Path(root or local_data_root() / "resources").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = json.loads(_MANIFEST.read_bytes())
        self.lock = threading.RLock()
        self.phase, self.error = "idle", ""
        self.completed, self.total = 0, 0
        self.checked_bytes = self.current_bytes = 0
        self.total_bytes = sum(
            spec["bytes"] for group in self.manifest["groups"].values() for spec in group["files"]
        )
        self.current_file = ""
        self.repaired = self.last_checked_at = 0
        self.public_images = self.native_images = self.image_bytes = 0
        self.version = self.manifest["version"]
        self.current = None
        self.latest_version = self.result = ""
        self.previous_version = ""
        self._image_cache = self._native = None
        self.client = PublicResourceClient()
        self.stop = threading.Event()
        self._thread = None

    def state(self):
        if self._image_cache is not None:
            self.describe_images(self._image_cache, self._native)
        with self.lock:
            return {
                "phase": self.phase,
                "error": self.error,
                "completed": self.completed,
                "total": self.total,
                "version": self.version,
                "latest_version": self.latest_version,
                "previous_version": self.previous_version,
                "result": self.result,
                "ready": self.current is not None,
                "current_file": self.current_file,
                "checked_bytes": self.checked_bytes,
                "current_bytes": self.current_bytes,
                "total_bytes": self.total_bytes,
                "repaired": self.repaired,
                "last_checked_at": self.last_checked_at,
                "public_images": self.public_images,
                "native_images": self.native_images,
                "image_bytes": self.image_bytes,
            }

    def use_existing(self, path):
        """仅接受加载器完整校验的公开字典目录；账号文件不在该目录契约中。"""
        from fgo_stat_core.infrastructure.report_master import load_report_master

        root = Path(path).resolve(strict=True)
        if str(root).startswith(("\\\\", "//")) or root.is_symlink():
            raise ExportError("LOCAL_DIRECTORY_INVALID")
        manifest = self._read_manifest(root)
        load_report_master(root / "data", root / "atlas-cn")
        if "atlas-jp" in manifest["groups"]:
            load_report_master(root / "data", root / "atlas-jp", region="JP")
        files = sum(len(group["files"]) for group in manifest["groups"].values())
        with self.lock:
            self.manifest = manifest
            self.version = manifest["version"]
            self.total_bytes = sum(
                spec["bytes"] for group in manifest["groups"].values() for spec in group["files"]
            )
            self.current = root
            self.phase, self.error = "ready", ""
            self.total = self.completed = files
            self.checked_bytes = self.total_bytes
        return root

    def restore_active(self):
        """重启优先使用上次核验并启用的版本，指针无效时保留内置资源。"""
        marker = self.root / "active.json"
        try:
            if marker.is_symlink() or marker.stat().st_size > 1024:
                return None
            value = json.loads(marker.read_bytes())
            name = value["directory"]
            if not isinstance(name, str) or not re.fullmatch(r"cn-[a-z0-9-]{1,120}", name):
                return None
            target = self.root / name
            if target.is_symlink() or target.resolve().parent != self.root:
                return None
            return self.use_existing(target)
        except (OSError, ValueError, KeyError, TypeError, ExportError):
            return None
        except Exception:
            # 字典加载器的兼容性错误也不能让可用的内置资源失效。
            return None

    def cache_existing(self, path):
        """将内置字典写入所选数据目录，再保存启用版本。"""
        root = self.use_existing(path)
        if root.parent != self.root:
            self._prepare(lambda _root: None, False)
            if self.phase == "error":
                raise ExportError(self.error)
        return self.current

    @staticmethod
    def _source_timestamps(root, manifest):
        version = json.loads((root / "data/version.json").read_bytes())
        timestamps = (
            version.get("timestamp"),
            manifest["groups"]["atlas-cn"]["atlas_info"].get("timestamp"),
            manifest["groups"].get("atlas-jp", {}).get("atlas_info", {}).get("timestamp", 0),
        )
        if any(type(value) is not int or not 0 <= value < 32_503_680_000 for value in timestamps):
            raise ExportError("RESOURCE_MANIFEST_INVALID")
        if not all(value > 0 for value in timestamps[:2]):
            raise ExportError("RESOURCE_MANIFEST_INVALID")
        return timestamps

    def restore_for_startup(self, bundle, *, cache_bundle=True):
        """新版内置字典比缓存完整且更新时直接启用，不等待联网检查。"""
        bundle = Path(bundle).resolve(strict=True)
        bundled_manifest = self._read_manifest(bundle)
        bundled_stamp = self._source_timestamps(bundle, bundled_manifest)
        active = self.restore_active()
        if active is not None:
            active_stamp = self._source_timestamps(active, self.manifest)
            # 所有已支持的源都不旧才替换，避免旧安装包覆盖已在线更新的字典。
            if not (
                all(new >= old for new, old in zip(bundled_stamp, active_stamp, strict=True))
                and bundled_stamp != active_stamp
            ):
                return active
        return self.cache_existing(bundle) if cache_bundle else self.use_existing(bundle)

    def _read_manifest(self, root):
        groups = {}
        selected = ["data", "atlas-cn"]
        if (root / "atlas-jp").exists() or (root / "atlas-jp").is_symlink():
            selected.append("atlas-jp")
        for group in selected:
            if (root / group).is_symlink():
                raise ExportError("RESOURCE_PATH_INVALID")
            path = root / group / "SOURCE_MANIFEST.json"
            if path.is_symlink() or path.stat().st_size > 256 * 1024:
                raise ExportError("RESOURCE_MANIFEST_INVALID")
            metadata = json.loads(path.read_bytes())
            files = metadata.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 192:
                raise ExportError("RESOURCE_MANIFEST_INVALID")
            names = set()
            for spec in files:
                self._check_spec(spec, complete=True)
                name = spec["filename"]
                if name in names or self._valid_file(root / group / name, spec) is None:
                    raise ExportError("RESOURCE_HASH_MISMATCH")
                names.add(name)
            groups[group] = metadata
        data = groups["data"]
        version = data.get("resource_version")
        if version and any(
            version != groups[group].get("resource_version") for group in selected[1:]
        ):
            raise ExportError("RESOURCE_MANIFEST_INVALID")
        if not version:
            revision = data.get("data_commit", "")
            info = groups["atlas-cn"].get("atlas_info", {})
            version = f"cn-bundled-{revision[:10]}-{info.get('hash', '')}-{info.get('dataVer', 0)}"
        if not isinstance(version, str) or not re.fullmatch(r"cn-[a-z0-9-]{1,120}", version):
            raise ExportError("RESOURCE_MANIFEST_INVALID")
        return {"version": version, "groups": groups}

    def describe_images(self, cache, native):
        """只记录公开资源的数量与体积，状态接口不返回本机路径。"""
        public = [p for p in cache.glob("*.png") if p.is_file() and not p.is_symlink()]
        original = (
            []
            if native is None
            else [p for p in native.rglob("*.png") if p.is_file() and not p.is_symlink()]
        )
        with self.lock:
            self._image_cache, self._native = cache, native
            self.public_images, self.native_images = len(public), len(original)
            self.image_bytes = sum(p.stat().st_size for p in public + original)

    def image_cache(self, bundle, dictionary=None):
        """将包内核验过的公开图片放入持久缓存，后续只下载缺少的图片。"""
        marker = bundle / "SEED_IMAGES_MANIFEST.json"
        # 图片按各自来源地址和内容摘要校验，不因名称字典更新重新下载整批图片。
        cache = self.root / "images" / "public-v2"
        cache.mkdir(parents=True, exist_ok=True)
        if cache.is_symlink():
            raise ExportError("RESOURCE_PATH_INVALID")
        if not marker.is_file():
            # 开发时可直接使用调用者明确选择的既有资源目录。
            existing = bundle / "assets"
            return existing if existing.is_dir() and not existing.is_symlink() else cache
        if marker.is_symlink() or marker.stat().st_size > 256 * 1024:
            raise ExportError("RESOURCE_MANIFEST_INVALID")
        entries = json.loads(marker.read_bytes())
        if not isinstance(entries, dict) or len(entries) > 3000:
            raise ExportError("RESOURCE_MANIFEST_INVALID")
        for name, digest in entries.items():
            if (
                not re.fullmatch(r"[a-z0-9-]+\.png(?:\.sha256)?", name)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ExportError("RESOURCE_MANIFEST_INVALID")
            source, target = bundle / "assets" / name, cache / name
            if source.is_symlink() or target.is_symlink():
                raise ExportError("RESOURCE_PATH_INVALID")
            if not source.is_file() or source.stat().st_size > 8 * 1024 * 1024:
                raise ExportError("RESOURCE_MANIFEST_INVALID")
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ExportError("RESOURCE_HASH_MISMATCH")
            if target.is_file():
                continue
            self._write(target, raw)
        return cache

    def prepare(self, finished, *, update=True):
        with self.lock:
            if self._thread and self._thread.is_alive():
                raise ExportError("OPERATION_NOT_READY")
            self.phase, self.error, self.completed = "discovering" if update else "checking", "", 0
            self.checked_bytes = self.current_bytes = self.repaired = 0
            self.current_file = "正在查询公开资源版本" if update else "正在校验资源"
            self.previous_version, self.latest_version, self.result = self.version, "", ""
            self.last_checked_at = 0
            self.total = sum(len(value["files"]) for value in self.manifest["groups"].values())
            self._thread = threading.Thread(
                target=self._prepare, args=(finished, update), daemon=True
            )
            self._thread.start()

    def _prepare(self, finished, update):
        stage = None
        try:
            seed = self.current
            catalog = discover_catalog(self.client, self.stop) if update else None
            manifest = catalog.manifest if catalog else json.loads(json.dumps(self.manifest))
            with self.lock:
                self.latest_version = manifest["version"]
                self.total = sum(len(g["files"]) for g in manifest["groups"].values())
                self.total_bytes = sum(
                    f.get("bytes", 0) for g in manifest["groups"].values() for f in g["files"]
                )
            stage = self.root / (".staging-" + uuid.uuid4().hex)
            stage.mkdir()
            seed_groups = self.manifest["groups"] if seed is not None else {}
            for group, metadata in manifest["groups"].items():
                directory = stage / group
                directory.mkdir(parents=True, exist_ok=True)
                if directory.is_symlink():
                    raise ExportError("RESOURCE_PATH_INVALID")
                for spec in metadata["files"]:
                    if self.stop.is_set():
                        raise ExportError("CANCELLED")
                    with self.lock:
                        self.phase, self.current_file, self.current_bytes = (
                            "checking",
                            spec["filename"],
                            0,
                        )
                    # 优先复用经过相同哈希校验的内置文件，不为一次检查重新下载整套字典。
                    path = directory / spec["filename"]
                    supplied = catalog.supplied.get((group, spec["filename"])) if catalog else None
                    if supplied is not None:
                        self._write(path, supplied)
                    elif seed is not None:
                        # Atlas 版本未变时复用之前核验过的完整哈希。
                        old_group = seed_groups.get(group, {})
                        if (
                            "sha256" not in spec
                            and "upstream_md5" not in spec
                            and metadata.get("atlas_info") == old_group.get("atlas_info")
                        ):
                            old_spec = next(
                                (
                                    s
                                    for s in old_group.get("files", [])
                                    if s["filename"] == spec["filename"]
                                ),
                                None,
                            )
                            if old_spec:
                                spec.update(bytes=old_spec["bytes"], sha256=old_spec["sha256"])
                                with self.lock:
                                    self.total_bytes += spec["bytes"]
                        old = seed / group / spec["filename"]
                        raw = self._valid_file(old, spec)
                        if raw is not None:
                            self._write(path, raw)
                    downloaded = self._file(directory, spec)
                    with self.lock:
                        self.completed += 1
                        self.checked_bytes += spec["bytes"]
                        self.current_bytes = 0
                        self.repaired += bool(downloaded)
                metadata.pop("_source_text", None)
                metadata["resource_version"] = manifest["version"]
                self._write(
                    directory / "SOURCE_MANIFEST.json",
                    (seed / group / "SOURCE_MANIFEST.json").read_bytes()
                    if not update and seed is not None
                    else json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode(),
                )
            with self.lock:
                self.phase, self.current_file = "validating", "正在校验完整字典与上游版本"
            from fgo_stat_core.contracts.models import CaptureError
            from fgo_stat_core.infrastructure.report_master import load_report_master

            self._read_manifest(stage)
            try:
                load_report_master(stage / "data", stage / "atlas-cn")
                if "atlas-jp" in manifest["groups"]:
                    load_report_master(stage / "data", stage / "atlas-jp", region="JP")
            except (CaptureError, AttributeError, IndexError):
                raise ExportError("RESOURCE_FORMAT_UNSUPPORTED") from None
            if catalog:
                catalog.verify_current(self.client, self.stop)
            if self.stop.is_set():
                raise ExportError("CANCELLED")
            same = (
                manifest["version"] == self.version
                and self.current is not None
                and self.current.parent == self.root
            )
            # 同一版本检查仍校验全部文件；文件损坏修复后才替换目录。
            if same:
                try:
                    self._read_manifest(seed)
                except Exception:
                    same = False
            if not same:
                with self.lock:
                    self.phase, self.current_file = "activating", "正在启用新资源"
                target = self.root / (manifest["version"] + "-" + uuid.uuid4().hex[:8])
                stage.rename(target)
                stage = target
                try:
                    finished(target)
                    self._write(
                        self.root / "active.json", json.dumps({"directory": target.name}).encode()
                    )
                except Exception:
                    if seed is not None:
                        finished(seed)
                    raise
                with self.lock:
                    self.current = target
                    self.manifest = manifest
                    self.version = manifest["version"]
                stage = None
            with self.lock:
                self.phase, self.error = "ready", ""
                self.result = "updated" if self.version != self.previous_version else "current"
                self.last_checked_at = int(time.time())
                self.current_file = ""
        except Exception as error:
            with self.lock:
                self.phase = "error"
                self.error = (
                    error.code if isinstance(error, ExportError) else "RESOURCE_PREPARE_FAILED"
                )
        finally:
            if stage is not None and stage.parent == self.root and not stage.is_symlink():
                # 只删除本次生成且已核对父目录的暂存目录，不清理已有版本或用户文件。
                shutil.rmtree(stage, ignore_errors=True)

    def _file(self, directory, spec):
        self._check_spec(spec)
        path = directory / spec["filename"]
        raw = self._valid_file(path, spec)
        downloaded = raw is None
        if downloaded:
            with self.lock:
                self.phase = "downloading"
            expected = spec.get("bytes", 16 * 1024 * 1024)
            raw = self.client.get(spec["url"], expected, progress=self._progress, stop=self.stop)
            if not self._matches(raw, spec):
                raise ExportError("RESOURCE_VERSION_CHANGED")
        try:
            value = json.loads(raw)
            if spec["filename"] == "items.json" and not isinstance(value, list):
                raise ValueError
        except (ValueError, RecursionError):
            raise ExportError("RESOURCE_FORMAT_UNSUPPORTED") from None
        if "bytes" not in spec:
            with self.lock:
                self.total_bytes += len(raw)
        spec.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        if downloaded:
            self._write(path, raw)
        return downloaded

    def _progress(self, count):
        with self.lock:
            self.current_bytes = count

    @staticmethod
    def _check_spec(spec, *, complete=False):
        name, url = spec["filename"], spec["url"]
        validate_url(url)
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", name)
            or (
                "bytes" in spec
                and (type(spec["bytes"]) is not int or not 0 < spec["bytes"] <= 16 * 1024 * 1024)
            )
            or ("sha256" in spec and not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"]))
            or (
                "upstream_md5" in spec and not re.fullmatch(r"[0-9a-f]{6,32}", spec["upstream_md5"])
            )
            or (complete and ("sha256" not in spec or "bytes" not in spec))
            or (
                "sha256" not in spec
                and "upstream_md5" not in spec
                and url
                not in {
                    "https://api.atlasacademy.io/repo/CN/master/mstGacha.json",
                    "https://api.atlasacademy.io/repo/CN/master/mstShop.json",
                    "https://api.atlasacademy.io/repo/JP/master/mstGacha.json",
                    "https://api.atlasacademy.io/repo/JP/master/mstShop.json",
                }
            )
        ):
            raise ExportError("RESOURCE_MANIFEST_INVALID")

    @staticmethod
    def _matches(raw, spec):
        return (
            ("bytes" not in spec or len(raw) == spec["bytes"])
            and ("sha256" not in spec or hashlib.sha256(raw).hexdigest() == spec["sha256"])
            and (
                "upstream_md5" not in spec
                or hashlib.md5(raw, usedforsecurity=False)
                .hexdigest()
                .startswith(spec["upstream_md5"])
            )
        )

    def _valid_file(self, path, spec):
        if path.is_symlink():
            raise ExportError("RESOURCE_PATH_INVALID")
        if not path.is_file() or "bytes" not in spec or path.stat().st_size != spec["bytes"]:
            return None
        raw = path.read_bytes()
        return raw if self._matches(raw, spec) else None

    @staticmethod
    def _write(path, raw):
        temp = path.with_name(path.name + ".partial")
        if path.is_symlink() or temp.is_symlink():
            raise ExportError("RESOURCE_PATH_INVALID")
        with temp.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
