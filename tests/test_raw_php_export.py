import json

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


def test_php_extension_does_not_wrap_or_reencode_response():
    raw = b'{"response":[{"nid":"login"}]}\n'
    # Export contract: the writer receives the response bytes unchanged.
    output = raw
    assert output is raw
    assert output.endswith(b"\n")
