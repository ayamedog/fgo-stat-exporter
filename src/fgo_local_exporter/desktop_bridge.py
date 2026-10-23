"""仅给本机窗口使用的文件选择、游戏准备及显式导出桥。"""

import base64
import functools
import io
import os
import re
import secrets
import sys
import threading
import webbrowser
from dataclasses import replace
from pathlib import Path

from PIL import Image

from fgo_local_exporter import __version__
from fgo_local_exporter.desktop_reports import DesktopReportBuilder
from fgo_local_exporter.desktop_resources import ResourceManager
from fgo_local_exporter.desktop_storage import StorageSettings, validate_data_directory
from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.game_profile import load_desktop_profile
from fgo_local_exporter.local_files import local_directory_path

__all__ = ["DesktopBridge"]


def _safe(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return {"ok": True, **(method(*args, **kwargs) or {})}
        except Exception as error:
            return {
                "ok": False,
                "error": error.code if isinstance(error, ExportError) else "LOCAL_OPERATION_FAILED",
            }

    return wrapped


class DesktopBridge:
    def __init__(self, config, resources, *, profile_path=None, data_root=None, storage=None):
        self._config, self._resources = config, resources
        self._store = self._window = self._session = None
        self._lock = threading.RLock()
        self._game_phase, self._game_error = "idle", ""
        self._last_folder = None
        self._profile_path = Path(profile_path) if profile_path is not None else None
        self._asset_root = None
        self._data_root = (
            Path(data_root)
            if data_root is not None
            else resources.root.parent
            if resources is not None
            else None
        )
        self._pending_data_root = None
        self._storage = storage or StorageSettings()
        self._selecting_directory = self._initializing_resources = False

    def _initialize_resources(self):
        """仅在已有数据目录时启用字典；复制和下载的进度由状态接口显示。"""
        resources = self._resources
        with self._lock:
            self._initializing_resources = True
        try:
            active = (
                resources.restore_for_startup(self._asset_root)
                if self._asset_root is not None
                else resources.restore_active()
            )
            if active is not None:
                self._attach_resources(active)
            resources.prepare(self._attach_resources)
        except Exception as error:
            with resources.lock:
                resources.phase = "error"
                resources.error = (
                    error.code if isinstance(error, ExportError) else "RESOURCE_PREPARE_FAILED"
                )
        finally:
            with self._lock:
                self._initializing_resources = False

    def _require_resources(self):
        with self._lock:
            if self._resources is None:
                raise ExportError("DATA_DIRECTORY_REQUIRED")
            if self._initializing_resources:
                raise ExportError("DATA_DIRECTORY_BUSY")
            return self._resources

    def _restore_profile(self):
        self._store.profile = None
        try:
            self._store.profile = load_desktop_profile(self._profile_path)
            self._game_phase = "ready" if self._store.profile is not None else "missing"
            self._game_error = "" if self._store.profile is not None else "LOCAL_PROFILE_REQUIRED"
        except ExportError as error:
            self._game_phase, self._game_error = "error", error.code
        self._store.profile_error = self._game_error

    @_safe
    def connect(self, csrf):
        if not isinstance(csrf, str) or len(csrf) > 100:
            raise ExportError("REQUEST_REJECTED")
        with self._store.lock:
            sessions = [
                s for s in self._store.sessions.values() if secrets.compare_digest(s.csrf, csrf)
            ]
        if len(sessions) != 1:
            raise ExportError("REQUEST_REJECTED")
        self._session = sessions[0]
        return self.status()

    @_safe
    def status(self):
        with self._lock:
            resources = self._resources
            resource_state = (
                resources.state() if resources is not None else {"phase": "waiting", "ready": False}
            )
            if self._initializing_resources and resource_state["phase"] == "idle":
                resource_state["phase"] = "checking"
            return {
                "desktop_version": __version__,
                "game_phase": self._game_phase,
                "game_error": self._game_error,
                "game_version": self._store.profile.version if self._store.profile else "",
                "resources": resource_state,
                "dictionary_directory": str(resources.current / "data")
                if resources is not None and resources.current is not None
                else "",
                "native_support": bool(self._config.native_ui_dir),
                "can_open_folder": self._last_folder is not None,
                "data_directory": str(self._data_root) if self._data_root is not None else "",
                "selecting_data_directory": self._selecting_directory,
                "pending_data_directory": str(self._pending_data_root)
                if self._pending_data_root is not None
                else "",
            }

    @_safe
    def prepare_resources(self):
        self._require_resources().prepare(self._attach_resources)

    @_safe
    def choose_resources(self):
        import webview

        resources = self._require_resources()
        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if paths:
            root = resources.use_existing(paths[0])
            self._asset_root = root
            self._attach_resources(root)

    @_safe
    def open_dictionary_folder(self):
        root = self._require_resources().current
        if root is None:
            raise ExportError("RESOURCE_NOT_READY")
        path = local_directory_path(root / "data")
        if not path.is_dir():
            raise ExportError("RESOURCE_NOT_READY")
        os.startfile(str(path))

    @_safe
    def choose_data_directory(self):
        import webview

        with self._lock:
            if self._selecting_directory:
                raise ExportError("DATA_DIRECTORY_BUSY")
            self._selecting_directory = True
        try:
            paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            if not paths:
                return {"changed": False}
            path = validate_data_directory(paths if isinstance(paths, str) else paths[0])
            self._storage.save(path)
            with self._lock:
                first_selection = self._data_root is None
                if first_selection:
                    self._resources = ResourceManager(path / "resources")
                    self._data_root = path
                self._pending_data_root = path if path != self._data_root else None
            if first_selection:
                self._initialize_resources()
            return {
                "changed": True,
                "restart_required": self._pending_data_root is not None,
                "first_selection": first_selection,
            }
        finally:
            with self._lock:
                self._selecting_directory = False

    @_safe
    def open_data_folder(self):
        if self._data_root is None:
            raise ExportError("DATA_DIRECTORY_REQUIRED")
        os.startfile(str(local_directory_path(self._data_root)))

    @_safe
    def open_documents(self):
        # 编译版只打开随 EXE 解包的说明目录，不读取用户传入的路径。
        if getattr(sys, "frozen", False):
            path = Path(sys._MEIPASS) / "documents"
        else:
            path = Path(__file__).resolve().parents[2]
        if not path.is_dir() or path.is_symlink():
            raise ExportError("DOCUMENTS_NOT_READY")
        os.startfile(str(path))

    @_safe
    def open_repository(self):
        """只打开项目的固定仓库地址，外部页面不进入应用窗口。"""
        if not webbrowser.open(
            "https://github.com/ayamedog/fgo-stat-exporter", new=2, autoraise=True
        ):
            raise ExportError("BROWSER_UNAVAILABLE")

    def _attach_resources(self, root):
        if self._asset_root is None:
            self._asset_root = root
        cache = self._resources.image_cache(self._asset_root, root)
        native = self._asset_root / "native-ui"
        if not (native / "font/FGO-Main-Font.otf").is_file():
            native = None
        windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        header = self._asset_root / "fonts/NotoSansCJK-Regular.ttc"
        regular = self._asset_root / "fonts/DejaVuSans.ttf"
        self._config = replace(
            self._config,
            data_dir=root / "data",
            atlas_dir=root / "atlas-cn",
            atlas_jp_dir=root / "atlas-jp" if (root / "atlas-jp").is_dir() else None,
            asset_dir=cache,
            native_ui_dir=native,
            header_font=header if header.is_file() else windows_fonts / "msyh.ttc",
            regular_font=regular if regular.is_file() else windows_fonts / "arial.ttf",
        )
        # 已有图片缓存也逐项经过现有读取器的哈希与 PNG 校验，不复制整个运行目录。
        builder = DesktopReportBuilder(self._config)
        with self._store.lock:
            self._store.builder = builder
        self._resources.describe_images(cache, native)

    def _ready_session(self):
        session = self._session
        if session is None:
            raise ExportError("VISITOR_EXPIRED")
        session.state()
        return session

    def _save(self, raw, filename, file_type):
        import webview

        path = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=filename, file_types=(file_type,)
        )
        if not path:
            return {"saved": False}
        path = local_directory_path(Path(path if isinstance(path, str) else path[0]))
        if path.suffix.lower() != Path(filename).suffix:
            raise ExportError("EXPORT_EXTENSION_INVALID")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            raise ExportError("EXPORT_PATH_EXISTS") from None
        self._last_folder = path.parent
        return {"saved": True, "name": path.name}

    @_safe
    def save_json(self):
        session = self._ready_session()
        with session.lock:
            if not session.export:
                raise ExportError("STATISTICS_NOT_READY")
            raw, server = session.export, session.server
            prefix = "fgo-jp" if session.preview.get("region") == "JP" else f"fgo-cn-{server}"
        return self._save(
            raw, f"{prefix}-statistics-{secrets.token_hex(3)}.json", "统计文件 (*.json)"
        )

    @_safe
    def save_image(self, value, filename, version):
        session = self._ready_session()
        with session.lock:
            if not session.document or session.report_version != version:
                raise ExportError("REPORT_EXPIRED")
        if (
            not isinstance(value, str)
            or len(value) > 28 * 1024 * 1024
            or not value.startswith("data:image/jpeg;base64,")
            or not isinstance(filename, str)
            or not re.fullmatch(
                r"fgo-(?:cn-(?:ios|android)|jp)-report(?:-[0-9]{2})?\.jpg", filename
            )
        ):
            raise ExportError("IMAGE_DATA_INVALID")
        raw = base64.b64decode(value.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if (
                image.format != "JPEG"
                or not 600 <= image.width <= 1800
                or not 1 <= image.height <= 28000
                or image.width * image.height > 50_000_000
            ):
                raise ExportError("IMAGE_DATA_INVALID")
            image.verify()
        return self._save(
            raw, filename[:-4] + "-" + secrets.token_hex(3) + ".jpg", "统计图片 (*.jpg)"
        )

    @_safe
    def open_folder(self):
        if self._last_folder is None:
            raise ExportError("EXPORT_NOT_SAVED")
        os.startfile(str(self._last_folder))
