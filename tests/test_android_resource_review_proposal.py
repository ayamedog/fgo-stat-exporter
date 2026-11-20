import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import prepare_android  # noqa: E402


def test_resource_review_proposal_can_prepare_default_public_source(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    work = tmp_path / "review-work"
    manifest = tmp_path / "review.json"
    payload = b'{"version":1}'

    def prepared_resources(value):
        assert value is None
        return source

    def copy_resources(_source, destination, _core):
        destination.mkdir(parents=True)
        target = destination / "data" / "version.json"
        target.parent.mkdir()
        target.write_bytes(payload)
        return {"data/version.json": hashlib.sha256(payload).hexdigest()}

    monkeypatch.setattr(prepare_android, "prepared_resources", prepared_resources)
    monkeypatch.setattr(prepare_android, "copy_resources", copy_resources)
    monkeypatch.setattr(
        prepare_android, "check_required_fonts_and_native_resources", lambda _public: None
    )

    prepare_android.propose_resource_review(None, work, manifest)

    assert (work / "reviewed-public-resources" / "data" / "version.json").read_bytes() == payload
    assert json.loads(manifest.read_text(encoding="utf-8")) == {
        "data/version.json": hashlib.sha256(payload).hexdigest()
    }
