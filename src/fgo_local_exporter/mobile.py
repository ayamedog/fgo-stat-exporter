"""安卓本机接口；复用统计引擎，文件选择与保存交给系统界面。"""

import base64
import io
import json
import logging
import re
import secrets
import socket
import threading
from dataclasses import replace
from pathlib import Path

from flask import Response
from PIL import Image
from werkzeug.serving import WSGIRequestHandler, make_server

from fgo_local_exporter.desktop_reports import DesktopReportBuilder
from fgo_local_exporter.desktop_resources import ResourceManager
from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.local_api import create_app
from fgo_local_exporter.settings import LocalConfig
from fgo_local_exporter.statistics import decode_response, export_statistics

__all__ = ["MobileRuntime", "bounded_import"]
_STATIC = Path(__file__).with_name("mobile_static")


def bounded_import(raw):
    """安卓不启动解释器子进程，沿用解码器的体积、深度和条目上限。"""
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise ExportError("INPUT_TOO_LARGE")
    return export_statistics(decode_response(raw), allow_jp=True)


class _SilentHandler(WSGIRequestHandler):
    def log(self, *_args, **_kwargs):
        pass


class MobileRuntime:
    def __init__(self, directory, bundle, profile=None):
        logging.disable(logging.CRITICAL)
        self._root = Path(directory)
        self._bundle = Path(bundle)
        self._resources = ResourceManager(self._root / "resources")
        self._session = None
        self._server = None
        self._resources.use_existing(self._bundle)
        self._config = LocalConfig(engine_source=Path(__file__).parents[1], max_sessions=1)
        for _ in range(5):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            self._config = replace(self._config, origin=f"http://127.0.0.1:{port}")
            app = create_app(self._config, importer=bounded_import, mobile=True)
            self._store = app.extensions["local_store"]
            try:
                self._server = make_server(
                    "127.0.0.1", port, app, threaded=True, request_handler=_SilentHandler
                )
                break
            except OSError:
                self._store.close()
        if self._server is None:
            raise ExportError("LOCAL_PORT_UNAVAILABLE")
        if profile:
            from fgo_local_exporter.game_profile import checked_profile

            self._store.profile = checked_profile(json.loads(profile))
        self._attach_resources(
            self._resources.restore_for_startup(self._bundle, cache_bundle=False)
        )

        @app.get("/desktop.js")
        def javascript():
            return Response((_STATIC / "mobile.js").read_bytes(), content_type="text/javascript")

        @app.get("/desktop.css")
        def stylesheet():
            shared = Path(__file__).with_name("desktop_static") / "desktop.css"
            return Response(
                shared.read_bytes() + (_STATIC / "mobile.css").read_bytes(), content_type="text/css"
            )

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._resources.prepare(self._attach_resources)

    def origin(self):
        return self._config.origin

    def connect(self, csrf):
        if not isinstance(csrf, str) or len(csrf) > 100:
            raise ExportError("REQUEST_REJECTED")
        with self._store.lock:
            found = [
                s for s in self._store.sessions.values() if secrets.compare_digest(s.csrf, csrf)
            ]
        if len(found) != 1:
            raise ExportError("REQUEST_REJECTED")
        self._session = found[0]

    def status(self):
        return json.dumps(
            {
                "ok": True,
                "resources": self._resources.state(),
                "dictionary_directory": str(self._resources.current / "data"),
            },
            ensure_ascii=False,
        )

    def prepare_resources(self):
        self._resources.prepare(self._attach_resources)

    def _attach_resources(self, root):
        cache = self._resources.image_cache(self._bundle, root)
        native = self._bundle / "native-ui"
        if not (native / "font/FGO-Main-Font.otf").is_file():
            native = None
        self._config = replace(
            self._config,
            data_dir=root / "data",
            atlas_dir=root / "atlas-cn",
            atlas_jp_dir=root / "atlas-jp" if (root / "atlas-jp").is_dir() else None,
            asset_dir=cache,
            native_ui_dir=native,
            header_font=self._bundle / "fonts/NotoSansCJK-Regular.ttc",
            regular_font=self._bundle / "fonts/DejaVuSans.ttf",
        )
        with self._store.lock:
            self._store.builder = DesktopReportBuilder(self._config)
        self._resources.describe_images(cache, native)

    def _ready(self):
        if self._session is None:
            raise ExportError("VISITOR_EXPIRED")
        self._session.state()
        return self._session

    def export_json(self):
        session = self._ready()
        with session.lock:
            if not session.export:
                raise ExportError("STATISTICS_NOT_READY")
            return session.export

    def remaining_seconds(self):
        session = self._ready()
        with session.lock:
            return session.state()["remaining_seconds"]

    def image_bytes(self, value, version):
        session = self._ready()
        with session.lock:
            if not session.document or session.report_version != version:
                raise ExportError("REPORT_EXPIRED")
        if (
            not isinstance(value, str)
            or len(value) > 28 * 1024 * 1024
            or not value.startswith("data:image/jpeg;base64,")
            or not re.fullmatch(r"[A-Za-z0-9+/=]+", value.split(",", 1)[1])
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
        return raw

    def clear(self):
        if self._session is not None:
            self._session.clear()

    def close(self):
        self._resources.stop.set()
        self._store.close()
        self._server.shutdown()
        self._server.server_close()
