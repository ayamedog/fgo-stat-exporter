"""密码登录仅使用人工数据，验证会话隔离和官方挑战回传时序。"""

import re
import threading
import time
from types import SimpleNamespace

import pytest

from fgo_local_exporter.local_api import create_app
from fgo_local_exporter.sessions import _PasswordRateLimiter

ORIGIN = "http://127.0.0.1:55313"


@pytest.fixture
def app():
    instance = create_app(profile=SimpleNamespace(version="synthetic"), cleanup=False)
    instance.extensions["local_store"].password_limiter = _PasswordRateLimiter()
    yield instance
    instance.extensions["local_store"].close()


def visitor(app):
    client = app.test_client()
    page = client.get("/", base_url=ORIGIN)
    token = re.search(rb'name="fgo-csrf" content="([^\"]+)"', page.data)[1].decode()
    return client, {"Origin": ORIGIN, "X-FGO-CSRF": token}


def post(client, path, headers, value):
    return client.post(path, base_url=ORIGIN, headers=headers, json=value)


def wait_state(client, wanted):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = client.get("/api/state", base_url=ORIGIN).json
        if value["phase"] in wanted:
            return value
        time.sleep(0.01)
    raise AssertionError("人工登录未在时限内结束")


def test_synthetic_password_challenge_and_generation_isolation(app, monkeypatch):
    result = {}

    class Client:
        def __init__(self, profile, server):
            assert server == "ios"

        def authenticate_password(self, account, password, cancelled, challenge):
            result.update(challenge({"gt": "synthetic-gt", "challenge": "synthetic-challenge"}))
            assert not cancelled()
            return {"uid": "1", "access_key": "synthetic-token"}

        def statistics(self, user, progress, cancelled):
            progress("fetching_statistics")
            return b"synthetic-statistics", {"region": "CN"}

    monkeypatch.setattr("fgo_local_exporter.sessions.GameClient", Client)
    client, headers = visitor(app)
    other, other_headers = visitor(app)
    state = client.get("/api/state", base_url=ORIGIN).json
    assert state["password_available"] is True
    value = {
        "server": "ios",
        "consent": True,
        "account": "synthetic-account",
        "password": "synthetic-password",
        "generation": state["generation"],
    }
    assert post(client, "/api/password", headers, value).status_code == 200
    state = wait_state(client, {"password_verification"})
    assert state["captcha_frame_url"] == "/captcha-frame.html"
    generation = state["generation"]
    assert (
        post(
            other, "/api/password/challenge", other_headers, {"generation": generation}
        ).status_code
        == 409
    )
    challenge = post(client, "/api/password/challenge", headers, {"generation": generation}).json
    assert challenge["gt"] == "synthetic-gt"
    assert (
        post(client, "/api/password/challenge", headers, {"generation": generation}).status_code
        == 409
    )
    verify = {
        "generation": generation,
        "challenge_id": challenge["challenge_id"],
        "result": {
            "challenge": "synthetic-challenge",
            "validate": "synthetic-validate",
            "seccode": "synthetic-seccode",
        },
    }
    assert post(client, "/api/password/verify", headers, verify).status_code == 200
    state = wait_state(client, {"complete", "error"})
    assert state["phase"] == "complete"
    assert result == verify["result"]
    assert post(client, "/api/password/verify", headers, verify).status_code == 409
    assert "synthetic-password" not in repr(state)


def test_cancel_wakes_challenge_and_stale_result_is_rejected(app, monkeypatch):
    finished = threading.Event()

    class Client:
        def __init__(self, profile, server):
            pass

        def authenticate_password(self, account, password, cancelled, challenge):
            try:
                return challenge({"gt": "synthetic-gt", "challenge": "synthetic-challenge"})
            finally:
                finished.set()

    monkeypatch.setattr("fgo_local_exporter.sessions.GameClient", Client)
    client, headers = visitor(app)
    value = {
        "server": "android",
        "consent": True,
        "account": "synthetic-account",
        "password": "synthetic-password",
        "generation": 0,
    }
    assert post(client, "/api/password", headers, value).status_code == 200
    state = wait_state(client, {"password_verification"})
    generation = state["generation"]
    assert (
        post(client, "/api/password/cancel", headers, {"generation": generation}).status_code == 200
    )
    assert finished.wait(2)
    assert (
        post(client, "/api/password/challenge", headers, {"generation": generation}).status_code
        == 409
    )
    assert client.get("/api/state", base_url=ORIGIN).json["phase"] == "cleared"


def test_password_api_requires_consent_csrf_and_unique_keys(app):
    client, headers = visitor(app)
    value = {
        "server": "android",
        "consent": False,
        "account": "synthetic-account",
        "password": "synthetic-password",
        "generation": 0,
    }
    response = post(client, "/api/password", headers, value)
    assert response.status_code == 409 and response.json["error"] == "CONSENT_REQUIRED"
    assert post(client, "/api/password", {}, value).status_code == 403
    response = client.post(
        "/api/password",
        base_url=ORIGIN,
        headers=headers,
        data=b'{"generation":0,"generation":1}',
        content_type="application/json",
    )
    assert response.status_code == 409 and response.json["error"] == "REQUEST_INVALID"


def test_captcha_frame_has_separate_sandbox_and_fixed_origin(app):
    client = app.test_client()
    frame = client.get(
        "/captcha-frame.html",
        base_url=ORIGIN,
        headers={"Sec-Fetch-Site": "cross-site", "Cookie": "synthetic=secret"},
    )
    assert frame.status_code == 200
    assert b'content="http://127.0.0.1:55313"' in frame.data
    assert "sandbox allow-scripts" in frame.headers["Content-Security-Policy"]
    assert "allow-same-origin" not in frame.headers["Content-Security-Policy"]
    assert "frame-ancestors http://127.0.0.1:55313" in frame.headers["Content-Security-Policy"]
    assert frame.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in frame.headers
    assert b"synthetic=secret" not in frame.data
    script = client.get(
        "/captcha-frame.js", base_url=ORIGIN, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert script.status_code == 200
    assert client.get("/captcha-frame.html?synthetic=1", base_url=ORIGIN).status_code == 403
    assert client.head("/captcha-frame.html", base_url=ORIGIN).status_code == 405
    assert (
        client.get(
            "/captcha-frame.html", base_url=ORIGIN, headers={"Authorization": "synthetic"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:1 policy",
        "http://localhost:abc",
        "http://127.0.0.1:0",
        "http://localhost:80",
        "http://localhost\nunsafe",
        "http://localhost:65536",
    ],
)
def test_config_rejects_noncanonical_origin(origin):
    from fgo_local_exporter.errors import ExportError
    from fgo_local_exporter.settings import LocalConfig

    with pytest.raises(ExportError, match="LOCAL_CONFIG_INVALID"):
        LocalConfig(origin=origin)


def test_password_limiter_does_not_store_account_or_password():
    from fgo_local_exporter.errors import ExportError

    limiter = _PasswordRateLimiter(account_limit=1)
    limiter.consume("synthetic-account")
    assert "synthetic-account" not in repr(limiter.accounts)
    with pytest.raises(ExportError, match="PASSWORD_RATE_LIMITED"):
        limiter.consume("synthetic-account")
