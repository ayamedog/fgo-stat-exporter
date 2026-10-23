"""只绑定本机回环地址的桌面接口；会话由 HttpOnly Cookie 隔离。"""

import atexit
import json
import re
import secrets
from pathlib import Path

from flask import Flask, Response, g, request
from werkzeug.exceptions import HTTPException

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.sessions import LocalStore
from fgo_local_exporter.settings import LocalConfig

__all__ = ["create_app"]

_STATIC = Path(__file__).parent / "ui"
_FILES = {
    "app.js": "text/javascript",
    "style.css": "text/css",
    "html-to-image.js": "text/javascript",
    "resource-status.js": "text/javascript",
    "gacha-records.js": "text/javascript",
    "gacha-records.css": "text/css",
}


def create_app(
    config=None, *, profile=None, builder=None, cleanup=True, importer=None, mobile=False
):
    config = config or LocalConfig()
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024 * 1024, DEBUG=False, TESTING=False)
    app.logger.disabled = True
    store = LocalStore(config, profile, builder, importer)
    app.extensions["local_store"] = store
    cookie = "fgo-visitor"

    if cleanup:
        store.start_cleanup()
        atexit.register(store.close)

    def reply(value, status=200):
        return Response(
            json.dumps(value, ensure_ascii=False),
            status=status,
            content_type="application/json; charset=utf-8",
        )

    @app.before_request
    def guard():
        if request.host != config.host:
            return reply({"error": "REQUEST_REJECTED"}, 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site" and request.path != "/":
            return reply({"error": "REQUEST_REJECTED"}, 403)
        origin = request.headers.get("Origin")
        if origin and origin != config.origin:
            return reply({"error": "REQUEST_REJECTED"}, 403)
        if request.path == "/" or request.path.startswith("/api/"):
            g.visitor = store.get(request.cookies.get(cookie, ""))
        if request.path.startswith("/api/"):
            if g.visitor is None:
                return reply({"error": "VISITOR_EXPIRED"}, 401)
            if request.method != "GET" and (
                origin != config.origin
                or not secrets.compare_digest(request.headers.get("X-FGO-CSRF", ""), g.visitor.csrf)
            ):
                return reply({"error": "REQUEST_REJECTED"}, 403)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
            "frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        if request.path.endswith("/view"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                "font-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
            )
        return response

    @app.errorhandler(ExportError)
    def export_error(error):
        return reply(
            {"error": error.code},
            429 if error.code == "LOCAL_BUSY" else 409,
        )

    @app.errorhandler(HTTPException)
    def http_error(error):
        return reply(
            {"error": "INPUT_TOO_LARGE" if error.code == 413 else "REQUEST_INVALID"},
            error.code or 400,
        )

    @app.errorhandler(Exception)
    def unexpected_error(_error):
        return reply({"error": "LOCAL_PROCESS_FAILED"}, 500)

    def visitor_page(filename):
        session = g.visitor or store.create()
        source = (_STATIC / filename).read_text(encoding="utf-8")
        if mobile:
            source = source.replace("Windows 桌面版", "安卓本机版")
        response = Response(
            source.replace("FGO_CSRF_VALUE", session.csrf).replace(
                "LOCAL_STATUS_PANEL",
                (Path(__file__).parent / "desktop_static/setup.html").read_text(encoding="utf-8"),
            ),
            content_type="text/html; charset=utf-8",
        )
        response.set_cookie(
            cookie,
            session.identifier,
            max_age=1800,
            secure=False,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    @app.get("/")
    def index():
        return visitor_page("index.html")

    @app.get("/static/<name>")
    def static_file(name):
        if name not in _FILES:
            return reply({"error": "NOT_FOUND"}, 404)
        return Response(
            (_STATIC / name).read_bytes(), content_type=_FILES[name] + "; charset=utf-8"
        )

    @app.get("/api/state")
    def state():
        return reply(g.visitor.state())

    def json_body(allowed):
        if (request.content_length or 0) > 2048:
            raise ExportError("REQUEST_INVALID")
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != set(allowed):
            raise ExportError("REQUEST_INVALID")
        return value

    def operation(callback):
        callback()
        return reply(g.visitor.state())

    @app.post("/api/start")
    def start():
        value = json_body({"server", "consent"})
        if value["consent"] is not True:
            raise ExportError("CONSENT_REQUIRED")
        return operation(lambda: g.visitor.start(value["server"]))

    @app.post("/api/import")
    def import_file():
        if request.mimetype != "application/octet-stream":
            raise ExportError("REQUEST_INVALID")
        if request.headers.get("X-FGO-Consent") != "yes":
            raise ExportError("CONSENT_REQUIRED")
        raw = request.get_data(cache=False)
        return operation(lambda: g.visitor.import_file(raw, request.headers.get("X-FGO-Server")))

    @app.post("/api/server")
    def select_server():
        value = json_body({"server", "generation"})
        return operation(lambda: g.visitor.select_server(value["server"], value["generation"]))

    @app.post("/api/clear")
    def clear():
        json_body(set())
        g.visitor.clear()
        return reply(g.visitor.state())

    @app.post("/api/report")
    def report():
        value = json_body({"friend", "support"})
        if any(type(v) is not bool for v in value.values()):
            raise ExportError("REQUEST_INVALID")
        return operation(lambda: g.visitor.build_report(value))

    @app.get("/api/qr.svg")
    def qr():
        with g.visitor.lock:
            if (
                request.args.get("generation") != str(g.visitor.generation)
                or not g.visitor.svg
            ):
                return reply({"error": "QR_NOT_READY"}, 404)
            return Response(g.visitor.svg, content_type="image/svg+xml")

    @app.get("/api/gachas")
    def gacha_history():
        from fgo_stat_core.contracts.models import CaptureError
        from fgo_stat_core.features.report.gacha_records import gacha_page

        if set(request.args) - {"q", "kind", "sort", "page"} or any(
            len(request.args.getlist(key)) != 1 for key in request.args
        ):
            raise ExportError("DETAILS_QUERY_INVALID")
        page = request.args.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,5}", page):
            raise ExportError("DETAILS_QUERY_INVALID")
        details, generation = g.visitor.gacha_history()
        try:
            value = gacha_page(
                details,
                query=request.args.get("q", ""),
                kind=request.args.get("kind", "all"),
                sort=request.args.get("sort", "default"),
                page=int(page),
            )
        except CaptureError as error:
            raise ExportError(error.code) from None
        with g.visitor.lock:
            g.visitor.state()
            if generation != g.visitor.generation or not g.visitor.export:
                raise ExportError("STATISTICS_NOT_READY")
            return reply({**value, "generation": generation})

    @app.get("/api/gachas/<int:gacha_id>/image")
    def gacha_image(gacha_id):
        generation = request.args.get("generation", "")
        if set(request.args) != {"generation"} or not re.fullmatch(r"[0-9]{1,12}", generation):
            raise ExportError("DETAILS_QUERY_INVALID")
        return Response(g.visitor.detail_image(int(generation), gacha_id), content_type="image/png")

    @app.get("/api/export")
    def export():
        with g.visitor.lock:
            g.visitor.state()
            if not g.visitor.export:
                raise ExportError("STATISTICS_NOT_READY")
            response = Response(g.visitor.export, content_type="application/json")
            filename = "fgo-jp-statistics.json" if g.visitor.preview.get("region") == "JP" else (
                f"fgo-cn-{g.visitor.server}-statistics.json"
            )
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

    def document(version):
        with g.visitor.lock:
            g.visitor.state()
            if not g.visitor.document or version != g.visitor.report_version:
                raise ExportError("REPORT_EXPIRED")
            return g.visitor.document

    @app.get("/api/report/<version>/data")
    def report_data(version):
        return reply(document(version).data)

    @app.get("/api/report/<version>/view")
    def report_view(version):
        source = document(version).html.replace("ASSET_BASE/", f"/api/report/{version}/assets/")
        return Response(source, content_type="text/html; charset=utf-8")

    @app.get("/api/report/<version>/assets/<key>")
    def report_asset(version, key):
        if not re.fullmatch(r"[0-9a-f]{24}\.png", key):
            return reply({"error": "NOT_FOUND"}, 404)
        raw = document(version).assets.get(key)
        if raw is None:
            return reply({"error": "NOT_FOUND"}, 404)
        return Response(raw, content_type="image/png")

    return app
