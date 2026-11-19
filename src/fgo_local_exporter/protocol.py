"""仅实现用户授权的游戏登录和统计读取，无奖励领取或游戏操作接口。"""

import base64
import hashlib
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.game_profile import GameProfile
from fgo_local_exporter.statistics import decode_response, export_statistics

__all__ = ["SERVERS", "GameClient"]
_SDK = "https://line3-sdk-center-login-sh.biligame.net"
# 原版 2.129.0 的网络清单分别列出安卓和 iOS 区服。
# 角色区服与授权所用 SDK 及本工具所在操作系统无关。
SERVERS = {
    "android": ("安卓服务器", "https://line3-s2-bili-fate.bilibiligame.net"),
    "ios": ("iOS 服务器", "https://line3-s2-ios-fate.bilibiligame.net"),
}
_LIMIT = 16 * 1024 * 1024


class _RawResponseError(ExportError):
    """内部携带可选的原始 toplogin 响应；异常文本仍只包含固定错误码。"""

    def __init__(self, code: str, raw_response: bytes):
        super().__init__(code)
        self.raw_response = raw_response


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ExportError("REMOTE_REDIRECT_REJECTED")


class GameClient:
    def authenticate_password(
        self,
        account: str,
        password: str,
        canceled: Callable[[], bool] | None = None,
        challenge_callback: Callable[[dict[str, str]], dict[str, str]] | None = None,
    ) -> dict:
        """最多一次在线 geetest 验证，其他挑战及风控受控停止。"""
        if not isinstance(account, str) or not 1 <= len(account) <= 256:
            raise ExportError("PASSWORD_INPUT_INVALID")
        ciphertext = None
        verification = None
        try:
            ciphertext = self._password_cipher_candidate(password, canceled)
            refresh_used = retry_used = challenge_used = False
            while True:
                result = self._password_login_candidate(account, ciphertext, canceled, verification)
                if verification is not None:
                    verification.clear()
                    verification = None
                if canceled is not None and canceled():
                    raise ExportError("PASSWORD_LOGIN_CANCELLED")
                code = str(result.get("code"))
                if (
                    code in {"200000", "200001", "200005", "200006", "200007"}
                    and challenge_callback is not None
                    and not challenge_used
                ):
                    challenge_used = True
                    verification = self._password_geetest_candidate(
                        canceled, challenge_callback, code
                    )
                    continue
                if code == "-662" and not refresh_used:
                    refresh_used = True
                    ciphertext = self._password_encrypt_candidate(password, result)
                    continue
                if code == "60010" and not retry_used:
                    retry_used = True
                    ciphertext = self._password_cipher_candidate(password, canceled)
                    continue
                return self._password_authorized_candidate(result)
        finally:
            if verification is not None:
                verification.clear()
            password = ciphertext = None

    def _password_sdk_candidate(self, path: str, extra: dict[str, str]) -> dict:
        """密码认证仅请求明确允许的官方接口。"""
        if path not in {
            "/api/external/issue/cipher/v3",
            "/api/external/login/v3",
            "/api/external/start_captcha/v3",
            "/api/client/start_captcha",
        }:
            raise ExportError("REMOTE_DESTINATION_REJECTED")
        params = dict(self.profile.sdk_base, timestamp=str(int(time.time() * 1000)))
        params.update(domain=urllib.parse.urlsplit(_SDK).hostname, **extra)
        params["sign"] = hashlib.md5(
            ("".join(params[k] for k in sorted(params)) + self.profile.sdk_key).encode()
        ).hexdigest()
        try:
            result = json.loads(self._request(_SDK + path, params))
            if not isinstance(result, dict):
                raise ExportError("SDK_RESPONSE_INVALID")
            return result
        except (ValueError, UnicodeError):
            raise ExportError("SDK_RESPONSE_INVALID") from None

    def _password_cipher_candidate(
        self, password: str, canceled: Callable[[], bool] | None = None
    ) -> str:
        """获取 RSA 公钥并返回 Base64 密文。"""
        if not isinstance(password, str) or not password or len(password) > 256:
            raise ExportError("PASSWORD_INPUT_INVALID")
        if canceled is not None and canceled():
            raise ExportError("PASSWORD_LOGIN_CANCELLED")
        result = self._password_sdk_candidate(
            "/api/external/issue/cipher/v3", {"cipher_type": "bili_login_rsa"}
        )
        if canceled is not None and canceled():
            raise ExportError("PASSWORD_LOGIN_CANCELLED")
        if str(result.get("code")) != "0":
            raise ExportError("PASSWORD_CIPHER_FAILED")
        return self._password_encrypt_candidate(password, result)

    @staticmethod
    def _password_encrypt_candidate(password: str, result: dict) -> str:
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA

        try:
            key = result.get("cipher_key")
            salt = result.get("hash")
            if (
                str(result.get("code")) not in {"0", "-662"}
                or not isinstance(key, str)
                or len(key) > 4096
                or not isinstance(salt, str)
                or len(salt) > 256
            ):
                raise ValueError
            public_key = RSA.import_key(key)
            if public_key.has_private() or not 1024 <= public_key.size_in_bits() <= 4096:
                raise ValueError
            payload = (salt + password).encode("utf-8")
            encrypted = PKCS1_v1_5.new(public_key).encrypt(payload)
            return base64.b64encode(encrypted).decode("ascii")
        except (ValueError, TypeError, UnicodeError, IndexError):
            raise ExportError("PASSWORD_CIPHER_FAILED") from None

    def _password_submit_candidate(
        self, account: str, ciphertext: str, canceled: Callable[[], bool] | None = None
    ) -> dict:
        """未启用：响应验证码时停止，绝不绕过或自动重试。"""
        result = self._password_login_candidate(account, ciphertext, canceled)
        return self._password_authorized_candidate(result)

    def _password_login_candidate(
        self,
        account: str,
        ciphertext: str,
        canceled: Callable[[], bool] | None = None,
        verification: dict[str, str] | None = None,
    ) -> dict:
        if not isinstance(account, str) or not 1 <= len(account) <= 256:
            raise ExportError("PASSWORD_INPUT_INVALID")
        if not isinstance(ciphertext, str) or not 1 <= len(ciphertext) <= 1024:
            raise ExportError("PASSWORD_CIPHER_FAILED")
        if canceled is not None and canceled():
            raise ExportError("PASSWORD_LOGIN_CANCELLED")
        fields = {"user_id": account, "pwd": ciphertext}
        fields.update(verification or {})
        try:
            return self._password_sdk_candidate("/api/external/login/v3", fields)
        finally:
            fields.clear()

    def _password_geetest_candidate(
        self,
        canceled: Callable[[], bool] | None,
        callback: Callable[[dict[str, str]], dict[str, str]],
        challenge_code: str = "200006",
    ) -> dict[str, str]:
        """挑战绑定、时限和用户操作由调用方负责。"""
        if canceled is not None and canceled():
            raise ExportError("PASSWORD_LOGIN_CANCELLED")
        if challenge_code == "200006":
            result = self._password_sdk_candidate(
                "/api/external/start_captcha/v3", {"captcha_category": "acctAntiFraud"}
            )
            supported_type = "geetest"
        elif challenge_code in {"200000", "200001", "200005", "200007"}:
            result = self._password_sdk_candidate("/api/client/start_captcha", {"version": "1"})
            supported_type = "1"
        else:
            raise ExportError("PASSWORD_VERIFICATION_REQUIRED")
        if canceled is not None and canceled():
            raise ExportError("PASSWORD_LOGIN_CANCELLED")
        if (
            str(result.get("code")) != "0"
            or str(result.get("captcha_type")) != supported_type
            or (supported_type == "1" and str(result.get("gs")) != "1")
        ):
            raise ExportError("PASSWORD_VERIFICATION_REQUIRED")

        def safe(value):
            return (
                isinstance(value, str)
                and 1 <= len(value) <= 1024
                and value.isascii()
                and all(32 < ord(c) < 127 for c in value)
            )

        token_field = "gt_user_id" if supported_type == "1" else "ctoken"
        gt, challenge, ctoken = (result.get(k) for k in ("gt", "challenge", token_field))
        if not all(safe(value) for value in (gt, challenge, ctoken)):
            raise ExportError("SDK_RESPONSE_INVALID")
        response = callback({"gt": gt, "challenge": challenge})
        if canceled is not None and canceled():
            raise ExportError("PASSWORD_LOGIN_CANCELLED")
        if (
            not isinstance(response, dict)
            or set(response) != {"challenge", "validate", "seccode"}
            or not all(safe(value) for value in response.values())
        ):
            raise ExportError("SDK_RESPONSE_INVALID")
        verification = {"captcha_type": supported_type, "ctoken": ctoken, **response}
        if supported_type == "1":
            verification["gt_user_id"] = ctoken
        return verification

    @staticmethod
    def _password_authorized_candidate(result: dict) -> dict:
        code = str(result.get("code"))
        if code in {"200000", "200001", "200005", "200006", "200007", "86154"}:
            raise ExportError("PASSWORD_VERIFICATION_REQUIRED")
        if code != "0":
            raise ExportError("PASSWORD_LOGIN_FAILED")
        uid, access_key = result.get("uid"), result.get("access_key")
        if (
            not isinstance(uid, (str, int))
            or isinstance(uid, bool)
            or not re.fullmatch(r"[0-9]{1,32}", str(uid))
        ):
            raise ExportError("SDK_CREDENTIAL_FIELDS_MISSING")
        if not isinstance(access_key, str) or not 1 <= len(access_key) <= 4096:
            raise ExportError("SDK_CREDENTIAL_FIELDS_MISSING")
        authorized = {"uid": uid, "access_key": access_key}
        for field in ("uname", "username"):
            value = result.get(field)
            if isinstance(value, str) and len(value) <= 256:
                authorized[field] = value
        return authorized

    def __init__(self, profile: GameProfile, server: str):
        if server not in SERVERS:
            raise ExportError("SERVER_SELECTION_REQUIRED")
        self.profile = profile
        self.server = server
        self.game_url = SERVERS[server][1]
        # 不继承系统代理；凭据仅通过经过证书校验的官方 TLS 连接发送。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, url: str, fields: dict) -> bytes:
        if not url.startswith((_SDK + "/", self.game_url + "/")):
            raise ExportError("REMOTE_DESTINATION_REJECTED")
        request = urllib.request.Request(
            url,
            urllib.parse.urlencode(fields).encode(),
            headers={
                "User-Agent": "Mozilla/5.0 BSGameSDK"
                if url.startswith(_SDK)
                else "Dalvik/2.1.0 (Linux; U; Android 9)",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Unity-Version": "2019.4.40f1",
            },
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                body = response.read(_LIMIT + 1)
            if len(body) > _LIMIT:
                raise ExportError("RESPONSE_LIMIT")
            return body
        except ExportError:
            raise
        except urllib.error.HTTPError as error:
            raise ExportError(f"REMOTE_HTTP_{int(error.code)}") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                code = "REMOTE_TLS_VERIFY_FAILED"
            elif isinstance(error.reason, TimeoutError):
                code = "REMOTE_TIMEOUT"
            else:
                code = "REMOTE_REQUEST_FAILED"
            raise ExportError(code) from None
        except TimeoutError:
            raise ExportError("REMOTE_TIMEOUT") from None
        except Exception:
            raise ExportError("REMOTE_REQUEST_FAILED") from None

    def _sdk(self, action: str, extra: dict | None = None) -> dict:
        params = dict(self.profile.sdk_base, timestamp=str(int(time.time() * 1000)))
        params.update(extra or {})
        params["sign"] = hashlib.md5(
            ("".join(params[k] for k in sorted(params)) + self.profile.sdk_key).encode()
        ).hexdigest()
        try:
            result = json.loads(
                self._request(_SDK + "/api/external/qrcode.login/" + action + "/v3", params)
            )
            if not isinstance(result, dict):
                raise ExportError("SDK_RESPONSE_INVALID")
            return result
        except (ValueError, UnicodeError):
            raise ExportError("SDK_RESPONSE_INVALID") from None

    def generate(self) -> tuple[str, str]:
        root = self._sdk("generate")
        data = root.get("data")
        if str(root.get("code")) != "0" or not isinstance(data, dict):
            raise ExportError("SDK_QR_GENERATE_FAILED")
        ticket = data.get("ticket")
        if not isinstance(ticket, str) or not 1 <= len(ticket) <= 1024:
            raise ExportError("SDK_RESPONSE_INVALID")
        redirect = data.get("redirect_url")
        if redirect:
            parsed = urllib.parse.urlsplit(redirect)
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or not parsed.hostname
                or parsed.port not in (None, 443)
                or not parsed.hostname.endswith(
                    (".biligame.com", ".biligame.net", ".bilibili.com", ".bilibiligame.net")
                )
            ):
                raise ExportError("SDK_QR_REDIRECT_REJECTED")
            value = (
                redirect
                + "?"
                + urllib.parse.urlencode(
                    {
                        "ticket": ticket,
                        "fromNative": "biligame://portal/auth/login/qrcode",
                        "id": str(data.get("game_base_id", "")),
                        "st": "0",
                    }
                )
            )
        else:
            value = "biligame://portal/auth/login/qrcode?" + ticket
        return ticket, value

    def poll(self, ticket: str) -> tuple[str, dict]:
        root = self._sdk("status", {"ticket": ticket})
        code = str(root.get("code"))
        if code == "0":
            data = root.get("data")
            if not isinstance(data, dict) or not data.get("access_key"):
                raise ExportError("SDK_CREDENTIAL_FIELDS_MISSING")
            return "authorized", {
                k: data[k] for k in ("uid", "access_key", "uname", "username") if k in data
            }
        state = {"710000": "waiting_scan", "710002": "waiting_confirm", "710001": "expired"}
        if code not in state:
            raise ExportError("SDK_QR_POLL_FAILED")
        return state[code], {}

    def _game_with_raw(
        self, path: str, params: dict, *, preserve_raw_on_error: bool = False
    ) -> tuple[dict, dict, bytes]:
        """验证游戏响应，同时保留 HTTP Response Body 的原始字节。"""
        raw = self._request(self.game_url + path, params)
        try:
            root = decode_response(raw)
            responses = root.get("response")
            if not isinstance(responses, list) or len(responses) != 1:
                raise ExportError("GAME_RESPONSE_INVALID")
            row = responses[0]
            code = str(row.get("resCode"))
            if code not in ("0", "00") or row.get("fail"):
                raise ExportError(
                    "GAME_REJECTED_" + code
                    if re.fullmatch(r"[0-9]{1,5}", code)
                    else "GAME_REJECTED"
                )
            if not isinstance(row.get("success"), dict):
                raise ExportError("GAME_RESPONSE_INVALID")
            return root, row["success"], raw
        except ExportError as error:
            if preserve_raw_on_error:
                raise _RawResponseError(error.code, raw) from None
            raise

    def _game(self, path: str, params: dict) -> tuple[dict, dict]:
        root, success, _ = self._game_with_raw(path, params)
        return root, success

    def statistics_with_raw(self, user: dict, progress, cancelled) -> tuple[bytes, dict, bytes]:
        """所有凭据只在此调用的内存中存活，调用结束后清除会话引用。"""
        member = params = info = login = None
        try:
            uid, access = str(user.get("uid", "")), user.get("access_key")
            if not uid.isdigit() or not isinstance(access, str) or not access:
                raise ExportError("SDK_CREDENTIAL_FIELDS_MISSING")
            common = {
                "deviceid": uuid.uuid4().hex,
                "os": "Android OS 9 / API-28",
                "ptype": "Android",
                "appVer": self.profile.version,
                "dateVer": "0",
                "lastAccessTime": str(int(time.time())),
                "try": "",
                "dataVer": "0",
                "developmentAuthCode": self.profile.development_code,
            }

            def stage(name):
                if cancelled():
                    raise ExportError("CANCELLED")
                progress(name)

            stage("game_handoff")
            member = dict(
                common,
                t="22360",
                v="1.0.1",
                s="1",
                mac="0",
                imei="",
                bundleid=_PACKAGE_NAME,
                username=str(user.get("uname", user.get("username", ""))),
                type="token",
                rkuid=uid,
                access_token=access,
                rksdkid="1",
                rkchannel="24",
                version="0",
            )
            _, info = self._game("/rongame_beta/rgfate/60_member/logintomembercenter.php", member)
            member.clear()
            access = None
            for name in ("dateVer", "dataVer"):
                value = info.get(name)
                if type(value) not in (int, str) or not re.fullmatch(r"[0-9]{1,12}", str(value)):
                    raise ExportError("GAME_RESPONSE_INVALID")
                common[name] = str(value)
            stage("game_initialization")
            params = dict(
                common,
                rgsid="1001",
                rguid=info["rguid"],
                rgusk=info["rgusk"],
                idfa="",
                v="1.0.1",
                mac="0",
                imei="",
                type="login",
                nickname=str(info["rgusk"])[:10],
                rkchannel="24",
                assetbundleFolder="",
                userAgent="1",
                t="20399",
                s="1",
                rksdkid="1",
            )
            _, login = self._game("/rongame_beta/rgfate/60_1001/login.php", params)
            params.clear()
            info.clear()
            stage("fetching_statistics")
            params = dict(
                common,
                ac="action",
                key="toplogin",
                userId=str(login["sguid"]),
                usk=hashlib.md5(
                    (self.profile.session_prefix + str(login["sgusk"])).encode()
                ).hexdigest(),
                umk="",
                rgsid="1001",
                rkchannel="24",
                userAgent="1",
                lastAccessTime="0",
                nickname=str(login.get("nickname", "")),
                sgtype=str(login.get("sgtype", "")),
                sgtag=str(login.get("sgtag", "")),
            )
            query = urllib.parse.urlencode({"_userId": str(login["sguid"]), "_key": "toplogin"})
            root, _, raw_toplogin = self._game_with_raw(
                "/rongame_beta/rgfate/60_1001/ac.php?" + query,
                params,
                preserve_raw_on_error=True,
            )
            params.clear()
            login.clear()
            stage("filtering_statistics")
            try:
                statistics_json, preview = export_statistics(root)
            except ExportError as error:
                raise _RawResponseError(error.code, raw_toplogin) from None
            except (KeyError, TypeError, ValueError):
                raise _RawResponseError("GAME_RESPONSE_INVALID", raw_toplogin) from None
            return statistics_json, preview, raw_toplogin
        except ExportError:
            raise
        except (KeyError, TypeError, ValueError):
            raise ExportError("GAME_RESPONSE_INVALID") from None
        finally:
            user.clear()
            for value in (member, params, info, login):
                if isinstance(value, dict):
                    value.clear()

    def statistics(self, user: dict, progress, cancelled) -> tuple[bytes, dict]:
        """向后兼容的过滤统计接口；原始响应由 statistics_with_raw 单独提供。"""
        body, preview, _ = self.statistics_with_raw(user, progress, cancelled)
        return body, preview


_PACKAGE_NAME = "com.bilibili.fatego"
