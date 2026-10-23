"""扫码状态机；凭据和统计只在本进程内存中短时保留。"""

import hashlib
import io
import secrets
import threading
import time

import qrcode
import qrcode.image.svg

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.protocol import SERVERS, GameClient

__all__ = ["LoginSession"]
_EXPORT_TTL = 600
_MESSAGES = {
    "preparing": "正在加载登录模块…",
    "ready": "请选择角色所在区服，再生成二维码。",
    "generating": "正在向官方游戏 SDK 申请二维码…",
    "waiting_scan": "请用官方客户端扫码，核对授权应用为“命运-冠位指定”。",
    "waiting_confirm": "已扫描，请在手机上核对应用并自行确认授权。",
    "game_handoff": "游戏授权已通过，正在连接 FGO…",
    "game_initialization": "正在初始化游戏登录…",
    "fetching_statistics": "正在读取游戏统计…",
    "filtering_statistics": "正在提取统计所需字段…",
    "complete": "统计已就绪，请核对区服、昵称和等级后保存。",
    "expired": "二维码已过期，请重新生成。",
    "cleared": "本次二维码和统计已清除。",
    "error": "本次操作未完成，请查看错误码。",
}
_ERRORS = {
    "REMOTE_REQUEST_FAILED": "无法连接官方服务，请检查网络后重试。",
    "GAME_REJECTED_88": "游戏拒绝完整登录，请重新扫码。",
    "SDK_QR_POLL_FAILED": "扫码被取消或官方服务拒绝，请重新生成二维码。",
    "SERVER_SELECTION_REQUIRED": "请先选择角色所在区服。",
    "STATISTICS_NOT_READY": "统计尚未就绪或已经过期，请重新扫码。",
}


class LoginSession:
    def __init__(self, profile):
        self.lock = threading.RLock()
        self.profile = profile
        self.csrf = secrets.token_urlsafe(32)
        self.generation = 0
        self.cancel = threading.Event()
        self.auth = {}
        self.svg = b""
        self.export = b""
        self.preview = None
        self.expires = 0.0
        self.phase = "preparing"
        self.error = ""
        self.server = ""

    def clear(self):
        with self.lock:
            self.generation += 1
            self.cancel.set()
            self.auth.clear()
            self.svg, self.export = (b"", b"")
            self.preview = None
            self.expires = 0.0
            self.phase, self.error = ("cleared", "")
            self.server = ""

    def state(self):
        with self.lock:
            if self.phase == "complete" and time.monotonic() >= self.expires:
                self.clear()
            return {
                "phase": self.phase,
                "message": _MESSAGES[self.phase],
                "error": self.error,
                "error_message": _ERRORS.get(self.error, "请记录错误码；不要发送原始响应或凭据。")
                if self.error
                else "",
                "version": self.profile.version if self.profile else None,
                "can_start": self.profile is not None
                and self.phase in {"ready", "cleared", "expired", "error", "complete"},
                "has_qr": bool(self.svg),
                "generation": self.generation,
                "preview": self.preview,
                "download_ready": bool(self.export),
                "remaining_seconds": max(0, int(self.expires - time.monotonic())),
                "server": self.server,
                "server_name": ("日服" if self.preview.get("region") == "JP" else "国服")
                if self.preview and getattr(self, "source", "") == "file"
                else SERVERS[self.server][0] if self.server else "",
            }

    def start(self, server: str):
        if server not in SERVERS:
            raise ExportError("SERVER_SELECTION_REQUIRED")
        with self.lock:
            if not self.state()["can_start"]:
                raise ExportError("OPERATION_NOT_READY")
            self.clear()
            self.cancel = threading.Event()
            generation = self.generation
            cancel = self.cancel
            self.phase = "generating"
            self.server = server
        threading.Thread(target=self._run, args=(generation, cancel, server), daemon=True).start()

    def _run(self, generation, cancel, server):
        user = {}

        def cancelled():
            return cancel.is_set() or generation != self.generation

        def progress(phase):
            with self.lock:
                if cancelled():
                    raise ExportError("CANCELLED")
                self.phase = phase
                if phase.startswith("game_") or phase in {"fetching_statistics", "expired"}:
                    self.svg = b""

        try:
            client = GameClient(self.profile, server)
            ticket, value = client.generate()
            qr = qrcode.make(value, image_factory=qrcode.image.svg.SvgPathImage)
            buffer = io.BytesIO()
            qr.save(buffer)
            with self.lock:
                if cancelled():
                    return
                self.svg = buffer.getvalue()
                self.phase = "waiting_scan"
                self.expires = time.monotonic() + 180
            del value, qr, buffer
            while not cancelled() and time.monotonic() < self.expires:
                phase, user = client.poll(ticket)
                if cancelled():
                    return
                if phase == "authorized":
                    with self.lock:
                        if cancelled():
                            return
                        self.auth = user
                        self.phase = "game_handoff"
                        self.svg = b""
                    body, preview = client.statistics(user, progress, cancelled)
                    with self.lock:
                        if cancelled():
                            return
                        self.export = body
                        self.preview = preview
                        self.preview["sha256"] = hashlib.sha256(body).hexdigest()
                        self.svg = b""
                        self.phase = "complete"
                        self.expires = time.monotonic() + _EXPORT_TTL
                        timer = threading.Timer(_EXPORT_TTL, self._expire, args=(generation,))
                        timer.daemon = True
                        timer.start()
                    return
                progress(phase)
                if phase == "expired":
                    return
                cancel.wait(2)
            if not cancelled():
                progress("expired")
        except Exception as error:
            with self.lock:
                if not cancelled():
                    self.failure_phase = self.phase
                    self.phase = "error"
                    self.error = (
                        error.code if isinstance(error, ExportError) else "LOCAL_PROCESS_FAILED"
                    )
                    self.export, self.svg, self.preview = (b"", b"", None)
        finally:
            user.clear()
            with self.lock:
                if generation == self.generation:
                    self.auth.clear()

    def _expire(self, generation):
        with self.lock:
            if generation == self.generation and self.phase == "complete":
                self.clear()
