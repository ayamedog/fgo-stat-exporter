import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "tools" / "android_preaudit_candidate")
)
from frozen_inputs import AuditFailure, verify_frozen  # noqa: E402
from propose_inputs import propose  # noqa: E402


def test_proposal_is_verifiable_but_not_called_approved(tmp_path):
    root = tmp_path / "jni"
    root.mkdir()
    (root / "libsample.so").write_bytes(b"synthetic ELF")
    output = tmp_path / "candidate.json"

    digest = propose({"jni": root}, output)
    manifest = json.loads(output.read_bytes())
    assert set(manifest) == {"version", "inputs"}
    assert manifest["inputs"]["jni"]["files"]["libsample.so"]["sha256"] == hashlib.sha256(
        b"synthetic ELF"
    ).hexdigest()
    verify_frozen({"jni": root}, output, digest)


def test_proposal_rejects_overlapping_roots_and_output_inside_input(tmp_path):
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    with pytest.raises(AuditFailure, match="INPUT_ROOT_OVERLAP"):
        propose({"root": root, "nested": nested}, tmp_path / "overlap.json")
    with pytest.raises(AuditFailure, match="MANIFEST_IN_INPUT"):
        propose({"root": root}, root / "candidate.json")


def test_proposal_never_overwrites_existing_review_material(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    output = tmp_path / "candidate.json"
    output.write_text("keep", encoding="utf-8")
    with pytest.raises(AuditFailure, match="OUTPUT_ALREADY_EXISTS"):
        propose({"input": root}, output)
    assert output.read_text(encoding="utf-8") == "keep"
