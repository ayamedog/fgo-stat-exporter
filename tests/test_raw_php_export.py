import json
import time

from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.login_session import LoginSession
from fgo_local_exporter.protocol import GameClient


def test_game_request_preserves_exact_raw_response_body():
    raw = (
        b'{ "response" : [ { "resCode" : "00", "success" : {"accepted":1} } ] }\r\n'
    )
    client = GameClient.__new__(GameClient)
    client.game_url = "https://example.invalid"
    client._request = lambda url, fields: raw

    decoded, success, original = client._game_with_raw("/test.php", {"key": "toplogin"})

    assert original == raw
    assert success == {"accepted": 1}
    assert decoded == json.loads(raw)


def test_raw_response_is_cleared_with_the_login_session():
    raw = b'{"response":[{"nid":"login"}]}\n'
    session = LoginSession(None)
    session.raw_response = raw

    assert session.state()["raw_ready"] is True

    session.clear()

    assert session.raw_response == b""
    assert session.state()["raw_ready"] is False



def test_invalid_toplogin_body_is_still_available_for_raw_export():
    raw = b'not-json\\r\\n'
    client = GameClient.__new__(GameClient)
    client.game_url = "https://example.invalid"
    client._request = lambda url, fields: raw

    try:
        client._game_with_raw("/ac.php?key=toplogin", {}, preserve_raw_on_error=True)
    except ExportError as error:
        assert error.raw_response == raw
    else:
        raise AssertionError("invalid response should not pass protocol validation")


def test_raw_only_session_state_is_downloadable_and_expires():
    session = LoginSession(None)
    session.phase = "raw_only"
    session.error = "GAME_RESPONSE_INVALID"
    session.raw_response = b'raw response'
    session.expires = time.monotonic() + 60

    state = session.state()
    assert state["raw_ready"] is True
    assert state["can_start"] is True
    assert state["message"].startswith("原始服务器响应已获取")

    session.clear()
    assert session.state()["raw_ready"] is False
