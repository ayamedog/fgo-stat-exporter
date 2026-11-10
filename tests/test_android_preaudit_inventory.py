import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "tools" / "android_preaudit_candidate")
)
from frozen_inputs import AuditFailure, verify_frozen  # noqa: E402


def test_reviewed_frozen_inputs_reject_unreviewed_changes(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    item = assets / "asset.txt"
    item.write_bytes(b"synthetic")
    manifest = tmp_path / "reviewed.json"
    source = {
        "version": 1,
        "inputs": {
            "assets": {
                "kind": "directory",
                "directories": [],
                "files": {
                    "asset.txt": {"size": 9, "sha256": hashlib.sha256(b"synthetic").hexdigest()}
                },
            }
        },
    }
    manifest.write_text(json.dumps(source), encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    verify_frozen({"assets": assets}, manifest, digest)
    (assets / "unexpected.txt").write_bytes(b"x")
    with pytest.raises(AuditFailure, match="FROZEN_INPUT_MISMATCH"):
        verify_frozen({"assets": assets}, manifest, digest)
    (assets / "unexpected.txt").unlink()
    item.write_bytes(b"different")
    with pytest.raises(AuditFailure, match="FROZEN_INPUT_MISMATCH"):
        verify_frozen({"assets": assets}, manifest, digest)
    with pytest.raises(AuditFailure, match="MANIFEST_HASH_INVALID"):
        verify_frozen({"assets": assets}, manifest, "0" * 64)
    with pytest.raises(AuditFailure, match="INPUT_SET_MISMATCH"):
        verify_frozen({}, manifest, digest)


def test_empty_directory_is_an_unauthorized_change(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    manifest = tmp_path / "reviewed.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "inputs": {"assets": {"kind": "directory", "directories": [], "files": {}}},
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    verify_frozen({"assets": assets}, manifest, digest)
    (assets / "extra").mkdir()
    with pytest.raises(AuditFailure, match="FROZEN_INPUT_MISMATCH"):
        verify_frozen({"assets": assets}, manifest, digest)


def test_optional_input_must_remain_absent(tmp_path):
    manifest = tmp_path / "reviewed.json"
    manifest.write_text(json.dumps({"version": 1, "inputs": {"profile": {"kind": "absent"}}}))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    profile = tmp_path / "baseline.prof"
    verify_frozen({"profile": profile}, manifest, digest)
    profile.write_bytes(b"synthetic profile")
    with pytest.raises(AuditFailure, match="FROZEN_INPUT_MISMATCH"):
        verify_frozen({"profile": profile}, manifest, digest)
