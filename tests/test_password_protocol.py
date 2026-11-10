"""离线人工数据验证未启用的密码协议原型。"""

import base64
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import pytest
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.protocol import GameClient


@pytest.fixture
def client():
    profile = SimpleNamespace(
        sdk_base={"game_id": "80002", "version": "80005"}, sdk_key="synthetic"
    )
    return GameClient(profile, "android")


def test_cipher_fetch_signing_and_encryption(client, monkeypatch):
    private = RSA.generate(1024)
    seen = []

    def request(url, fields):
        seen.append((url, fields.copy()))
        return json.dumps(
            {
                "code": 0,
                "cipher_key": private.public_key().export_key().decode(),
                "hash": "synthetic-salt",
            }
        ).encode()

    monkeypatch.setattr(client, "_request", request)
    value = client._password_cipher_candidate("synthetic-password")
    assert (
        PKCS1_v1_5.new(private).decrypt(base64.b64decode(value), b"bad")
        == b"synthetic-saltsynthetic-password"
    )
    url, fields = seen[0]
    assert url.endswith("/api/external/issue/cipher/v3")
    assert fields["cipher_type"] == "bili_login_rsa"
    assert fields["domain"] == "line3-sdk-center-login-sh.biligame.net"
    sign = fields.pop("sign")
    assert (
        sign
        == hashlib.md5(
            ("".join(fields[k] for k in sorted(fields)) + "synthetic").encode()
        ).hexdigest()
    )
    assert "synthetic-password" not in repr(fields)


def test_submit_encoded_ciphertext_and_filter_response(client, monkeypatch):
    observed = []

    def request(url, fields):
        observed.append((url, fields.copy()))
        return json.dumps(
            {
                "code": 0,
                "uid": "123",
                "access_key": "synthetic-token",
                "uname": "synthetic-name",
                "username": True,
                "unknown": "discard",
            }
        ).encode()

    monkeypatch.setattr(client, "_request", request)
    assert client._password_submit_candidate("synthetic-account", "a+/=") == {
        "uid": "123",
        "access_key": "synthetic-token",
        "uname": "synthetic-name",
    }
    url, fields = observed[0]
    assert url.endswith("/api/external/login/v3")
    assert fields["pwd"] == "a+/="
    assert parse_qs(urlencode(fields))["pwd"] == ["a+/="]
    sign = fields.pop("sign")
    assert (
        sign
        == hashlib.md5(
            ("".join(fields[k] for k in sorted(fields)) + "synthetic").encode()
        ).hexdigest()
    )


@pytest.mark.parametrize("code", [200000, 200001, 200005, 200006, 200007])
def test_captcha_stops_without_retry(client, monkeypatch, code):
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda url, fields: calls.append(url) or json.dumps({"code": code}).encode(),
    )
    with pytest.raises(ExportError, match="^PASSWORD_VERIFICATION_REQUIRED$"):
        client._password_submit_candidate("synthetic-account", "synthetic-ciphertext")
    assert len(calls) == 1


@pytest.mark.parametrize("code", [-662, 60010])
def test_risk_and_cipher_refresh_codes_fail_without_retry(client, monkeypatch, code):
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda url, fields: calls.append(url) or json.dumps({"code": code}).encode(),
    )
    with pytest.raises(ExportError, match="^PASSWORD_LOGIN_FAILED$"):
        client._password_submit_candidate("synthetic-account", "synthetic-ciphertext")
    assert len(calls) == 1


@pytest.mark.parametrize("nested", [False, True])
def test_official_risk_url_is_never_returned_or_requested(client, monkeypatch, nested):
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")
    calls = []
    untrusted_url = "https://synthetic.invalid/verify?credential=synthetic-secret"

    def sdk(path, fields):
        calls.append(path)
        return {
            "code": 86154,
            **(
                {"data": {"verify_url": untrusted_url}} if nested else {"verify_url": untrusted_url}
            ),
        }

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    with pytest.raises(ExportError, match="^PASSWORD_VERIFICATION_REQUIRED$") as error:
        client.authenticate_password("synthetic-account", "synthetic-password")
    assert str(error.value) == "PASSWORD_VERIFICATION_REQUIRED"
    assert calls == ["/api/external/login/v3"]
    assert untrusted_url not in str(error.value)


def test_password_login_normal_flow(client, monkeypatch):
    private = RSA.generate(1024)
    calls = []

    def sdk(path, fields):
        calls.append(path)
        if path.endswith("issue/cipher/v3"):
            return {
                "code": 0,
                "cipher_key": private.public_key().export_key().decode(),
                "hash": "salt",
            }
        assert fields["user_id"] == "synthetic-account"
        assert PKCS1_v1_5.new(private).decrypt(base64.b64decode(fields["pwd"]), b"bad") == (
            b"saltsynthetic-password"
        )
        return {"code": 0, "uid": "123", "access_key": "synthetic-token"}

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    assert client.authenticate_password("synthetic-account", "synthetic-password") == {
        "uid": "123",
        "access_key": "synthetic-token",
    }
    assert calls == ["/api/external/issue/cipher/v3", "/api/external/login/v3"]


def test_password_key_refresh_is_bounded(client, monkeypatch):
    first, second = RSA.generate(1024), RSA.generate(1024)
    calls = []

    def sdk(path, fields):
        calls.append(path)
        if path.endswith("issue/cipher/v3"):
            return {"code": 0, "cipher_key": first.public_key().export_key().decode(), "hash": "a"}
        if calls.count("/api/external/login/v3") == 1:
            return {
                "code": -662,
                "cipher_key": second.public_key().export_key().decode(),
                "hash": "b",
            }
        assert PKCS1_v1_5.new(second).decrypt(base64.b64decode(fields["pwd"]), b"bad") == (
            b"bsynthetic-password"
        )
        return {"code": -662, "cipher_key": second.public_key().export_key().decode(), "hash": "b"}

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    with pytest.raises(ExportError, match="^PASSWORD_LOGIN_FAILED$"):
        client.authenticate_password("synthetic-account", "synthetic-password")
    assert calls.count("/api/external/login/v3") == 2


def test_password_60010_refetch_is_bounded(client, monkeypatch):
    calls = []
    cipher_calls = []

    def cipher(password, canceled):
        cipher_calls.append(True)
        return "cipher"

    monkeypatch.setattr(client, "_password_cipher_candidate", cipher)

    def sdk(path, fields):
        calls.append(path)
        return {"code": 60010}

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    with pytest.raises(ExportError, match="^PASSWORD_LOGIN_FAILED$"):
        client.authenticate_password("synthetic-account", "synthetic-password")
    assert calls == ["/api/external/login/v3", "/api/external/login/v3"]
    assert len(cipher_calls) == 2


def test_cancel_after_login_response_stops_retry(client, monkeypatch):
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")
    monkeypatch.setattr(client, "_password_login_candidate", lambda *args: {"code": 60010})
    with pytest.raises(ExportError, match="^PASSWORD_LOGIN_CANCELLED$"):
        client.authenticate_password("synthetic-account", "synthetic-password", lambda: True)


def test_geetest_challenge_replays_original_ciphertext_once(client, monkeypatch):
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")
    calls = []
    callback_values = []

    def sdk(path, fields):
        calls.append((path, fields.copy()))
        if path.endswith("start_captcha/v3"):
            return {
                "code": 0,
                "captcha_type": "geetest",
                "gt": "mock-gt",
                "challenge": "mock-original",
                "ctoken": "mock-private-token",
            }
        if sum(p.endswith("login/v3") for p, _ in calls) == 1:
            return {"code": 200006}
        return {"code": 0, "uid": "123", "access_key": "synthetic-token"}

    def challenge(parameters):
        callback_values.append(parameters)
        return {
            "challenge": "mock-returned",
            "validate": "mock-validation",
            "seccode": "mock-seccode",
        }

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    assert client.authenticate_password(
        "synthetic-account", "synthetic-password", None, challenge
    ) == {"uid": "123", "access_key": "synthetic-token"}
    assert callback_values == [{"gt": "mock-gt", "challenge": "mock-original"}]
    assert calls[-1][1] == {
        "user_id": "synthetic-account",
        "pwd": "cipher",
        "captcha_type": "geetest",
        "ctoken": "mock-private-token",
        "challenge": "mock-returned",
        "validate": "mock-validation",
        "seccode": "mock-seccode",
    }
    assert len(calls) == 3


@pytest.mark.parametrize("challenge_type", ["img", "1", "2", "unknown"])
def test_unsupported_captcha_type_does_not_invoke_callback(client, monkeypatch, challenge_type):
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")
    calls = []

    def sdk(path, fields):
        if path.endswith("start_captcha/v3"):
            return {"code": 0, "captcha_type": challenge_type}
        return {"code": 200006}

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    with pytest.raises(ExportError, match="^PASSWORD_VERIFICATION_REQUIRED$"):
        client.authenticate_password("synthetic-account", "synthetic-password", None, calls.append)
    assert not calls


def test_repeated_challenge_stops_without_second_callback(client, monkeypatch):
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")
    calls = []
    monkeypatch.setattr(
        client, "_password_geetest_candidate", lambda canceled, cb, code: calls.append(True) or {}
    )
    monkeypatch.setattr(client, "_password_sdk_candidate", lambda path, fields: {"code": 200006})
    with pytest.raises(ExportError, match="^PASSWORD_VERIFICATION_REQUIRED$"):
        client.authenticate_password("synthetic-account", "synthetic-password", None, lambda v: v)
    assert len(calls) == 1


@pytest.mark.parametrize("trigger", [200000, 200001, 200005, 200007])
def test_legacy_online_geetest_uses_v1_and_replays_ctoken_and_user_id(client, monkeypatch, trigger):
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")
    calls = []
    challenges = []

    def sdk(path, fields):
        calls.append((path, fields.copy()))
        if path == "/api/client/start_captcha":
            return {
                "code": 0,
                "captcha_type": 1,
                "gs": 1,
                "gt": "synthetic-gt",
                "challenge": "synthetic-challenge",
                "gt_user_id": "synthetic-id",
            }
        if len(calls) == 1:
            return {"code": trigger}
        return {"code": 0, "uid": "123", "access_key": "synthetic-token"}

    def callback(value):
        challenges.append(value)
        return {
            "challenge": "synthetic-returned",
            "validate": "synthetic-validation",
            "seccode": "synthetic-seccode",
        }

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    assert client.authenticate_password(
        "synthetic-account", "synthetic-password", None, callback
    ) == {"uid": "123", "access_key": "synthetic-token"}
    assert calls[1] == ("/api/client/start_captcha", {"version": "1"})
    assert challenges == [{"gt": "synthetic-gt", "challenge": "synthetic-challenge"}]
    assert calls[2][1] == {
        "user_id": "synthetic-account",
        "pwd": "cipher",
        "captcha_type": "1",
        "ctoken": "synthetic-id",
        "gt_user_id": "synthetic-id",
        "challenge": "synthetic-returned",
        "validate": "synthetic-validation",
        "seccode": "synthetic-seccode",
    }


@pytest.mark.parametrize("kind,gs", [("1", 0), ("2", 1), ("img", 1), ("geetest", 1)])
def test_legacy_unsupported_or_offline_challenge_is_rejected(client, monkeypatch, kind, gs):
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")
    calls = []

    def sdk(path, fields):
        if path == "/api/client/start_captcha":
            return {"code": 0, "captcha_type": kind, "gs": gs}
        return {"code": 200000}

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    with pytest.raises(ExportError, match="^PASSWORD_VERIFICATION_REQUIRED$"):
        client.authenticate_password("synthetic-account", "synthetic-password", None, calls.append)
    assert calls == []


def test_legacy_challenge_v1_signing(client, monkeypatch):
    seen = []

    def request(url, fields):
        seen.append((url, fields.copy()))
        return json.dumps(
            {
                "code": 0,
                "captcha_type": 1,
                "gs": 1,
                "gt": "artificial-gt",
                "challenge": "artificial-original",
                "gt_user_id": "artificial-id",
            }
        ).encode()

    monkeypatch.setattr(client, "_request", request)

    def callback(_):
        return {
            "challenge": "artificial-result",
            "validate": "artificial-validation",
            "seccode": "artificial-security",
        }

    client._password_geetest_candidate(None, callback, "200000")
    url, fields = seen[0]
    assert url.endswith("/api/client/start_captcha") and fields["version"] == "1"
    sign = fields.pop("sign")
    assert (
        sign
        == hashlib.md5(
            ("".join(fields[k] for k in sorted(fields)) + "synthetic").encode()
        ).hexdigest()
    )


def test_legacy_challenge_is_not_reused_after_key_refresh(client, monkeypatch):
    calls = []
    callback_calls = []
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")
    monkeypatch.setattr(client, "_password_encrypt_candidate", lambda password, result: "refresh")

    def sdk(path, fields):
        calls.append((path, fields.copy()))
        if path == "/api/client/start_captcha":
            return {
                "code": 0,
                "captcha_type": 1,
                "gs": 1,
                "gt": "mock-gt",
                "challenge": "mock-original",
                "gt_user_id": "mock-id",
            }
        login_count = sum(item[0] == "/api/external/login/v3" for item in calls)
        return {"code": [200000, -662, 200000][login_count - 1]}

    def callback(parameters):
        callback_calls.append(parameters)
        return {
            "challenge": "mock-result",
            "validate": "mock-validation",
            "seccode": "mock-security",
        }

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    with pytest.raises(ExportError, match="^PASSWORD_VERIFICATION_REQUIRED$"):
        client.authenticate_password("synthetic-account", "synthetic-password", None, callback)
    assert len(callback_calls) == 1
    assert calls[2][1]["ctoken"] == "mock-id"
    assert calls[3][1] == {"user_id": "synthetic-account", "pwd": "refresh"}
    assert len(calls) == 4


def test_geetest_replay_includes_verification_fields_in_signature(client, monkeypatch):
    fields_seen = []

    def request(url, fields):
        fields_seen.append(fields.copy())
        return b'{"code":0,"uid":"123","access_key":"synthetic-token"}'

    monkeypatch.setattr(client, "_request", request)
    verification = {
        "captcha_type": "geetest",
        "ctoken": "artificial-token",
        "challenge": "artificial-challenge",
        "validate": "artificial-validation",
        "seccode": "artificial-seccode",
    }
    assert (
        client._password_login_candidate(
            "synthetic-account", "synthetic-cipher", None, verification
        )["code"]
        == 0
    )
    fields = fields_seen[0]
    sign = fields.pop("sign")
    assert all(fields[key] == value for key, value in verification.items())
    assert (
        sign
        == hashlib.md5(
            ("".join(fields[k] for k in sorted(fields)) + "synthetic").encode()
        ).hexdigest()
    )


def test_cancel_after_verification_does_not_replay(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_password_cipher_candidate", lambda password, canceled: "cipher")

    def sdk(path, fields):
        calls.append(path)
        if path.endswith("start_captcha/v3"):
            return {
                "code": 0,
                "captcha_type": "geetest",
                "gt": "mock-gt",
                "challenge": "mock-original",
                "ctoken": "mock-token",
            }
        return {"code": 200006}

    cancelled = False

    def callback(parameters):
        nonlocal cancelled
        cancelled = True
        return {
            "challenge": "mock-returned",
            "validate": "mock-validation",
            "seccode": "mock-seccode",
        }

    monkeypatch.setattr(client, "_password_sdk_candidate", sdk)
    with pytest.raises(ExportError, match="^PASSWORD_LOGIN_CANCELLED$"):
        client.authenticate_password(
            "synthetic-account", "synthetic-password", lambda: cancelled, callback
        )
    assert calls == ["/api/external/login/v3", "/api/external/start_captcha/v3"]


def test_cancel_before_requests(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_request", lambda url, fields: calls.append(url))
    with pytest.raises(ExportError, match="^PASSWORD_LOGIN_CANCELLED$"):
        client._password_cipher_candidate("synthetic-password", canceled=lambda: True)
    with pytest.raises(ExportError, match="^PASSWORD_LOGIN_CANCELLED$"):
        client._password_submit_candidate(
            "synthetic-account", "synthetic-ciphertext", canceled=lambda: True
        )
    assert calls == []


def test_cancel_after_cipher_fetch(client, monkeypatch):
    monkeypatch.setattr(client, "_password_sdk_candidate", lambda path, fields: {})
    states = iter((False, True))
    with pytest.raises(ExportError, match="^PASSWORD_LOGIN_CANCELLED$"):
        client._password_cipher_candidate("synthetic-password", canceled=lambda: next(states))
