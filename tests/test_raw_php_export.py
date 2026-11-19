import json

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
