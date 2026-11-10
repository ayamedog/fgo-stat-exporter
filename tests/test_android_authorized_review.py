"""仅使用人工构造数据，验证本次 Android 页面例外不会扩大发布范围。"""

import hashlib
import importlib
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

SYNTH_NATIVE_ASSET = b"synthetic native image"
SYNTH_NATIVE_FONT = b"synthetic approved font"
SYNTH_NOTO = b"synthetic noto font"
SYNTH_DEJAVU = b"synthetic dejavu font"


def native_contracts():
    hashes = {"font/synthetic.png": hashlib.sha256(SYNTH_NATIVE_ASSET).hexdigest()}
    font = {
        "key": "font/FGO-Main-Font.otf",
        "sha256": hashlib.sha256(SYNTH_NATIVE_FONT).hexdigest(),
        "bytes": len(SYNTH_NATIVE_FONT),
    }
    return json.dumps(hashes).encode(), json.dumps(font).encode()


def override_font_hashes(reviewer, monkeypatch):
    monkeypatch.setattr(
        reviewer,
        "FONT_HASHES",
        {
            "fonts/NotoSansCJK-Regular.ttc": hashlib.sha256(SYNTH_NOTO).hexdigest(),
            "fonts/DejaVuSans.ttf": hashlib.sha256(SYNTH_DEJAVU).hexdigest(),
        },
    )


@pytest.fixture
def reviewer(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    return importlib.import_module("review_android")


def synthetic_zip(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return data.getvalue()


def synthetic_public_resources():
    raw = b"{}"
    singles = {
        "version.json",
        "items.json",
        "entities.json",
        "mappingPatch.json",
        "classBoards.json",
        "grandGraphs.json",
        "constData.json",
        "baseSkills.json",
    }
    groups = {
        "data": singles
        | {
            name + ".1.json"
            for name in ("servants", "craftEssences", "wars", "wiki.servants", "mappingData")
        },
        "atlas-cn": {"mstGacha.json", "mstGachaCNExtra.json", "mstShop.json"},
        "atlas-jp": {"mstGacha.json", "mstShop.json"},
    }
    resources = {
        "SEED_IMAGES_MANIFEST.json": b"{}",
        "native-ui/font/synthetic.png": SYNTH_NATIVE_ASSET,
        "native-ui/font/FGO-Main-Font.otf": SYNTH_NATIVE_FONT,
        "fonts/NotoSansCJK-Regular.ttc": SYNTH_NOTO,
        "fonts/DejaVuSans.ttf": SYNTH_DEJAVU,
    }
    for group, names in groups.items():
        for name in names:
            resources[group + "/" + name] = raw
        resources[group + "/SOURCE_MANIFEST.json"] = json.dumps(
            {
                "files": [
                    {"filename": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                    for name in sorted(names)
                ]
            }
        ).encode()
    return resources


def synthetic_signature(unsigned, review):
    data = bytearray(unsigned)
    central = struct.unpack_from("<I", data, len(data) - 6)[0]
    value = b"synthetic signature"
    pair = struct.pack("<QI", len(value) + 4, 0x7109871A) + value
    size = len(pair) + 24
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + review.APK_MAGIC
    signed = data[:central] + block + data[central:]
    struct.pack_into("<I", signed, len(signed) - 6, central + len(block))
    expected = {"7109871a": {"size": len(value), "sha256": review.digest(value)}}
    return bytes(signed), expected


def scan(review, data, signing, prepared, probes=()):
    inventory = {}
    allowed = {review.APP_ARCHIVE + "!/" + item for item in review.AUTHORIZED_PAGES}
    review._scan_zip(
        data,
        "",
        0,
        inventory,
        allowed,
        prepared,
        {},
        {},
        signing,
        [b"artificial-sdk-secret-9876"],
        [b"artificial-signing-secret-7654"],
        set(probes),
        {"size": 0, "count": 0},
    )
    return inventory


def test_authorization_is_exactly_fourteen_reviewed_page_paths(reviewer):
    assert len(reviewer.AUTHORIZED_PAGES) == 14
    assert "fgo_local_exporter/ui/captcha-frame.html" in reviewer.AUTHORIZED_PAGES
    assert "fgo_local_exporter/ui/captcha-frame.js" in reviewer.AUTHORIZED_PAGES
    allowed = {reviewer.APP_ARCHIVE + "!/" + name for name in reviewer.AUTHORIZED_PAGES}
    assert not reviewer._name_allowed(
        "fgo_local_exporter/ui/app.js",
        reviewer.APP_ARCHIVE + "!/fgo_local_exporter/ui/app.js",
        allowed,
    )
    with pytest.raises(reviewer.AuditError, match="UNAUTHORIZED_PAGE_SOURCE"):
        reviewer._name_allowed("app.js", "assets/app.js", allowed)
    with pytest.raises(reviewer.AuditError, match="SOURCE_OR_PRIVATE_FILE"):
        reviewer._name_allowed("backend.py", "assets/backend.py", allowed)


def test_authorized_page_must_match_exact_reviewed_content(reviewer, tmp_path):
    name = "fgo_local_exporter/ui/app.js"
    source = tmp_path / "python" / name
    source.parent.mkdir(parents=True)
    source.write_bytes(b"// artificial approved page")
    inner = synthetic_zip([(name, source.read_bytes())])
    apk, signing = synthetic_signature(synthetic_zip([(reviewer.APP_ARCHIVE, inner)]), reviewer)
    inventory = scan(reviewer, apk, signing, tmp_path, [source.read_bytes()])
    assert reviewer.APP_ARCHIVE + "!/" + name in inventory
    source.write_bytes(b"// different artificial page")
    with pytest.raises(reviewer.AuditError, match="AUTHORIZED_RESOURCE_MISMATCH"):
        scan(reviewer, apk, signing, tmp_path)


@pytest.mark.parametrize(
    "name", ["assets/app.js", "assets/source.py", "../outside", "assets/game-protocol.json.copy.js"]
)
def test_other_source_and_unsafe_paths_are_rejected(reviewer, tmp_path, name):
    apk, signing = synthetic_signature(synthetic_zip([(name, b"synthetic")]), reviewer)
    with pytest.raises(reviewer.AuditError):
        scan(reviewer, apk, signing, tmp_path)


def test_sdk_value_allowed_only_in_fixed_profile_asset(reviewer, tmp_path):
    secret = b"artificial-sdk-secret-9876"
    config = tmp_path / reviewer.PROFILE_ASSET
    config.parent.mkdir(parents=True)
    config.write_bytes(json.dumps({"sdk_key": secret.decode()}).encode())
    apk, signing = synthetic_signature(
        synthetic_zip([(reviewer.PROFILE_ASSET, config.read_bytes())]), reviewer
    )
    assert reviewer.PROFILE_ASSET in scan(reviewer, apk, signing, tmp_path)
    apk, signing = synthetic_signature(synthetic_zip([("assets/notice.txt", secret)]), reviewer)
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        scan(reviewer, apk, signing, tmp_path)


def test_sensitive_value_not_allowed_even_in_profile(reviewer, tmp_path):
    secret = b"artificial-signing-secret-7654"
    config = tmp_path / reviewer.PROFILE_ASSET
    config.parent.mkdir(parents=True)
    config.write_bytes(secret)
    apk, signing = synthetic_signature(synthetic_zip([(reviewer.PROFILE_ASSET, secret)]), reviewer)
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        scan(reviewer, apk, signing, tmp_path)


def test_source_disguised_in_nested_notice_is_rejected(reviewer, tmp_path):
    source = b"function artificial_project_source(){return 'test-only';}\n" * 8
    inner = synthetic_zip([("notice.txt", source)])
    apk, signing = synthetic_signature(synthetic_zip([("assets/manual.zip", inner)]), reviewer)
    with pytest.raises(reviewer.AuditError, match="PROJECT_SOURCE_FOUND"):
        scan(reviewer, apk, signing, tmp_path, [source])


def test_duplicate_or_unknown_signing_block_is_rejected(reviewer, tmp_path):
    with pytest.warns(UserWarning):
        zipped = synthetic_zip([("assets/note.txt", b"a"), ("assets/note.txt", b"b")])
    apk, signing = synthetic_signature(zipped, reviewer)
    with pytest.raises(reviewer.AuditError, match="ZIP_DUPLICATE_NAME"):
        scan(reviewer, apk, signing, tmp_path)
    apk, signing = synthetic_signature(synthetic_zip([("assets/note.txt", b"a")]), reviewer)
    signing["12345678"] = signing.pop("7109871a")
    with pytest.raises(reviewer.AuditError, match="APK_SIGNATURE_BLOCK_MISMATCH"):
        scan(reviewer, apk, signing, tmp_path)


def test_complete_stage_rejects_unlisted_files_and_secret_copies(reviewer, tmp_path):
    data = b"artificial-sdk-secret-9876"
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "note.txt").write_bytes(b"safe synthetic text")
    expected = {"note.txt": reviewer._record(b"safe synthetic text")}
    reviewer._tree(stage, expected, [data], [b"artificial-signing-secret-7654"])
    (stage / "unexpected.txt").write_bytes(b"unexpected")
    with pytest.raises(reviewer.AuditError, match="STAGE_MANIFEST_MISMATCH"):
        reviewer._tree(stage, expected, [data], [b"artificial-signing-secret-7654"])
    (stage / "unexpected.txt").unlink()
    (stage / "note.txt").write_bytes(data)
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        reviewer._tree(stage, expected, [data], [b"artificial-signing-secret-7654"])


def test_public_resource_allowlist_cannot_be_expanded_by_editing_manifest(
    reviewer, tmp_path, monkeypatch
):
    root = tmp_path / "project"
    monkeypatch.setattr(reviewer, "ROOT", root)
    override_font_hashes(reviewer, monkeypatch)
    contract = root / "src/fgo_stat_core/infrastructure/native_support_ui"
    contract.mkdir(parents=True)
    hashes, font = native_contracts()
    (contract / "asset_hashes.json").write_bytes(hashes)
    (contract / "font_manifest.json").write_bytes(font)
    assets = tmp_path / "assets"
    assets.mkdir()
    resources = synthetic_public_resources()
    resources["unexpected/private.txt"] = b"artificial"
    (assets / "public-resources.zip").write_bytes(synthetic_zip(list(resources.items())))
    (assets / "public-manifest.json").write_text(
        json.dumps({name: reviewer.digest(value) for name, value in resources.items()})
    )
    with pytest.raises(reviewer.AuditError, match="PUBLIC_RESOURCE_MISMATCH"):
        reviewer._review_public_resources(
            tmp_path, {name: reviewer.digest(value) for name, value in resources.items()}
        )


def test_nested_public_resource_secret_is_rejected_before_zip_packaging(reviewer, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    resources = synthetic_public_resources()
    resources["data/version.json"] = b"artificial-sdk-secret-9876"
    metadata = json.loads(resources["data/SOURCE_MANIFEST.json"])
    for entry in metadata["files"]:
        if entry["filename"] == "version.json":
            entry["sha256"] = reviewer.digest(resources["data/version.json"])
            entry["bytes"] = len(resources["data/version.json"])
    resources["data/SOURCE_MANIFEST.json"] = json.dumps(metadata).encode()
    (assets / "public-resources.zip").write_bytes(synthetic_zip(list(resources.items())))
    review_map = {name: reviewer.digest(value) for name, value in resources.items()}
    (assets / "public-manifest.json").write_text(json.dumps(review_map))
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        reviewer._review_public_resources(
            tmp_path, review_map, profile_secret=[b"artificial-sdk-secret-9876"]
        )


def test_signing_alias_is_a_complete_token_not_a_url_substring(reviewer):
    alias = reviewer.SigningAlias(b"syntheticalias")
    reviewer.scan_secret(b'<a href="https://example.invalid/syntheticalias-extra.css">', [alias])
    for payload in (
        b'keyAlias="syntheticalias"',
        b'{"keyAlias":"syntheticalias"}',
        b"syntheticalias",
    ):
        with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
            reviewer.scan_secret(payload, [alias])
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        reviewer.scan_secret(b"https://example.invalid/syntheticalias-extra.css", [bytes(alias)])


def test_signing_alias_only_allows_exact_reviewed_repository_anchor(
    reviewer, tmp_path, monkeypatch
):
    alias = reviewer.SigningAlias(b"syntheticrepo")
    page = (
        b'<a id="source-repository" class="repository-link" '
        b'href="https://github.com/ayamedog/syntheticrepo">repository</a>'
    )
    project = tmp_path / "project"
    source = project / "src/fgo_local_exporter/ui/index.html"
    source.parent.mkdir(parents=True)
    source.write_bytes(page)
    monkeypatch.setattr(reviewer, "ROOT", project)
    stage_path = "python/fgo_local_exporter/ui/index.html"
    apk_path = reviewer.APP_ARCHIVE + "!/fgo_local_exporter/ui/index.html"

    reviewer.scan_secret(page, [alias], context=stage_path)
    reviewer.scan_secret(page, [alias], context=apk_path)
    for context, payload in (
        ("assets/other.html", page),
        (reviewer.APP_ARCHIVE + "!/other/index.html", page),
        (stage_path, page + b' {"alias":"syntheticrepo"}'),
        (apk_path, page + b' keyAlias="syntheticrepo"'),
        (stage_path, b'<a id="other" href="https://github.com/ayamedog/syntheticrepo">'),
    ):
        with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
            reviewer.scan_secret(payload, [alias], context=context)

    # Even if a newly reviewed page repeats the alias elsewhere, only the one href is exempt.
    duplicate = page + b' {"alias":"syntheticrepo"}'
    source.write_bytes(duplicate)
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        reviewer.scan_secret(duplicate, [alias], context=stage_path)
    source.write_bytes(page)
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        reviewer.scan_secret(page, [alias, bytes(alias)], context=stage_path)
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        reviewer.scan_secret(b"https://example.invalid/syntheticrepo", [alias])


def test_signing_alias_exception_requires_exact_nested_apk_page(reviewer, tmp_path, monkeypatch):
    alias = reviewer.SigningAlias(b"syntheticrepo")
    page_name = "fgo_local_exporter/ui/index.html"
    page = (
        b'<a id="source-repository" '
        b'href="https://github.com/ayamedog/syntheticrepo">repository</a>'
    )
    project = tmp_path / "project"
    source = project / "src" / page_name
    source.parent.mkdir(parents=True)
    source.write_bytes(page)
    monkeypatch.setattr(reviewer, "ROOT", project)
    staged = tmp_path / "stage/python" / page_name
    staged.parent.mkdir(parents=True)
    staged.write_bytes(page)

    def inspect(entries):
        inner = synthetic_zip([(page_name, page)])
        apk, signing = synthetic_signature(
            synthetic_zip([(reviewer.APP_ARCHIVE, inner), *entries]), reviewer
        )
        reviewer._scan_zip(
            apk, "", 0, {}, {reviewer.APP_ARCHIVE + "!/" + page_name}, tmp_path / "stage",
            {}, {}, signing, [], [alias], set(), {"size": 0, "count": 0},
        )

    inspect([])
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        inspect([("assets/notice.txt", b'{"alias":"syntheticrepo"}')])
    staged.write_bytes(page + b"modified")
    with pytest.raises(reviewer.AuditError, match="AUTHORIZED_RESOURCE_MISMATCH"):
        inspect([])


def test_signing_alias_exception_only_covers_verified_dex_repository_string(
    reviewer, tmp_path, monkeypatch
):
    alias = reviewer.SigningAlias(b"syntheticrepo")
    url = b"https://github.com/ayamedog/syntheticrepo"
    project = tmp_path / "project"
    source = project / "android/app/src/main/java/io/github/ayamedog/fgostats/MainActivity.java"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'private static final String SOURCE_REPOSITORY = "' + url + b'";')
    monkeypatch.setattr(reviewer, "ROOT", project)

    def dex_string(value):
        assert len(value) < 128
        data = bytearray(116)
        data[:8] = b"dex\n035\0"
        struct.pack_into("<II", data, 56, 1, 112)
        struct.pack_into("<I", data, 112, 116)
        return bytes(data) + bytes([len(value)]) + value + b"\0"

    valid = dex_string(url)
    reviewer.scan_secret(valid, [alias], context="classes.dex")
    for context, payload in (
        ("classes2.dex", valid),
        ("classes.dex", valid + b'{"alias":"syntheticrepo"}'),
        ("classes.dex", dex_string(b"https://example.invalid/syntheticrepo")),
        ("classes.dex", b"not a dex " + url),
        ("classes.dex", valid + url),
    ):
        with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
            reviewer.scan_secret(payload, [alias], context=context)
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        reviewer.scan_secret(valid, [alias, bytes(alias)], context="classes.dex")
    source.write_bytes(b"// URL absent from the reviewed Java source")
    with pytest.raises(reviewer.AuditError, match="SECRET_FOUND"):
        reviewer.scan_secret(valid, [alias], context="classes.dex")


def test_android_metadata_allows_only_implied_legacy_read_permission(reviewer, monkeypatch):
    expected = {
        "application_id": "io.github.ayamedog.fgostats",
        "version_code": "18",
        "version_name": "0.4.15",
        "min_sdk": "24",
        "target_sdk": "35",
        "permissions": ["android.permission.INTERNET", "android.permission.WRITE_EXTERNAL_STORAGE"],
    }
    badging = "\n".join((
        "package: name='io.github.ayamedog.fgostats' versionCode='18' versionName='0.4.15'",
        "sdkVersion:'24'",
        "targetSdkVersion:'35'",
        "uses-permission: name='android.permission.INTERNET'",
        "uses-permission: name='android.permission.WRITE_EXTERNAL_STORAGE' maxSdkVersion='28'",
        "uses-permission: name='android.permission.READ_EXTERNAL_STORAGE' maxSdkVersion='28'",
        "uses-implied-permission: name='android.permission.READ_EXTERNAL_STORAGE' "
        "maxSdkVersion='28' reason='requested WRITE_EXTERNAL_STORAGE'",
        "",
    ))
    xml = "\n".join((
        "    E: uses-permission (line=11)",
        '      A: android:name(0x01010003)="android.permission.INTERNET" '
        '(Raw: "android.permission.INTERNET")',
        "    E: uses-permission (line=12)",
        '      A: android:name(0x01010003)="android.permission.WRITE_EXTERNAL_STORAGE" '
        '(Raw: "android.permission.WRITE_EXTERNAL_STORAGE")',
        "      A: android:maxSdkVersion(0x01010271)=(type 0x10)0x1c",
        "    E: application (line=16)",
        "",
    ))

    def inspect(badging_text, xml_text):
        def fake_run(args, **_kwargs):
            text = badging_text if args[2] == "badging" else xml_text
            fields = {"returncode": 0, "stdout": text.replace("\n", "\r\n").encode()}
            return type("Result", (), fields)()
        monkeypatch.setattr(reviewer.subprocess, "run", fake_run)
        return reviewer.verify_android_metadata(Path("aapt"), Path("candidate.apk"), expected)

    assert inspect(badging, xml)
    assert not inspect(
        badging.replace("reason='requested WRITE_EXTERNAL_STORAGE'", "reason='other'"), xml
    )
    assert not inspect(badging.replace("maxSdkVersion='28'", "maxSdkVersion='29'", 1), xml)
    assert not inspect(badging + "uses-permission: name='android.permission.CAMERA'\n", xml)
    assert not inspect(badging, xml + "    E: uses-permission (line=99)\n")
    assert not inspect(badging, xml.replace("0x1c", "0x1d"))
    altered = xml.replace(
        "    E: application", "    E: uses-permission-sdk-23 (line=3)\n    E: application"
    )
    assert not inspect(badging, altered)


def test_source_probe_allows_only_verified_report_css_constant(reviewer, tmp_path, monkeypatch):
    import importlib.util
    import marshal

    prepare = importlib.import_module("prepare_android")
    style = (".synthetic-report { color: #123456; }\n" * 28).encode()
    monkeypatch.setattr(reviewer, "REPORT_STYLE_SHA256", reviewer.digest(style))
    name = "fgo_stat_core/infrastructure/report_ui/builder.pyc"
    source = tmp_path / "builder.py"
    stage = tmp_path / "stage"
    staged = stage / "python" / name
    staged.parent.mkdir(parents=True)
    full = reviewer.APP_ARCHIVE + "!/" + name
    probe = style[:100]

    def compile_staged(extra=""):
        original = ("def build():\n    return " + repr(style.decode()) + "\n" + extra).encode()
        source.write_bytes(original)
        code = prepare._strip_debug(compile(original, "<test>", "exec"))
        data = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(code)
        staged.write_bytes(data)
        return data

    data = compile_staged()
    span = reviewer._verified_report_css_span(data, full, stage, {name: source})
    reviewer._scan_source(data, {probe}, allowed_span=span)
    with pytest.raises(reviewer.AuditError, match="PROJECT_SOURCE_FOUND"):
        reviewer._scan_source(data, {probe})
    extra = "def other():\n    return " + repr(probe.decode()) + "\n"
    data = compile_staged(extra)
    span = reviewer._verified_report_css_span(data, full, stage, {name: source})
    with pytest.raises(reviewer.AuditError, match="PROJECT_SOURCE_FOUND"):
        reviewer._scan_source(data, {probe}, allowed_span=span)
    with pytest.raises(reviewer.AuditError, match="APP_CONTENT_MISMATCH"):
        reviewer._verified_report_css_span(data + b"tampered", full, stage, {name: source})
    assert reviewer._verified_report_css_span(data, "other/path.pyc", stage, {name: source}) is None


def test_zipimport_marker_exception_cannot_hide_a_real_archive(reviewer, monkeypatch):
    import importlib.util
    import marshal

    prepare = importlib.import_module("prepare_android")
    marker = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 1, 1, 16, 0x7FFFFFFF, 0)
    code = prepare._strip_debug(compile("MARKER = " + repr(marker), "<test>", "exec"))
    fake = importlib.util.MAGIC_NUMBER + b"\0" * 12 + marshal.dumps(code)
    path = "assets/chaquopy/stdlib-common.imy!/zipimport.pyc"
    monkeypatch.setattr(reviewer, "ZIPIMPORT_SHA256", reviewer.digest(fake))
    assert zipfile.is_zipfile(io.BytesIO(fake))
    assert reviewer._verified_zipimport_marker(fake, path)
    assert not reviewer._verified_zipimport_marker(fake, "other/path/zipimport.pyc")
    assert not reviewer._verified_zipimport_marker(fake + b"modified", path)

    with_extra_archive = fake + synthetic_zip([("extra.txt", b"unreviewed")])
    monkeypatch.setattr(reviewer, "ZIPIMPORT_SHA256", reviewer.digest(with_extra_archive))
    assert zipfile.is_zipfile(io.BytesIO(with_extra_archive))
    with pytest.raises(reviewer.AuditError, match="PYC_TRAILING_DATA"):
        reviewer._verified_zipimport_marker(with_extra_archive, path)


def test_zip_extras_allow_only_exact_zero_alignment_padding(reviewer):
    name = "lib/example.so"
    offset = 100
    name_length = len(name)
    padding = (-(offset + 30 + name_length)) % 16384
    reviewer._verify_zip_extras(name, 0, 0, offset, name_length, b"\0" * padding, b"")
    reviewer._verify_zip_extras("nested.pyc", 1, 8, 0, 10, b"", b"")
    invalid = (
        (name, 0, 0, offset, name_length, b"\0" * (padding - 1), b""),
        (name, 0, 0, offset, name_length, b"\0" * (padding - 1) + b"\1", b""),
        (name, 0, 0, offset, name_length, b"\0" * padding, b"\x34\x12\0\0"),
        ("nested.pyc", 1, 8, 0, 10, b"\0\0\0\0", b""),
        ("asset.txt", 0, 8, 0, 9, b"\0\0\0\0", b""),
    )
    for args in invalid:
        with pytest.raises(reviewer.AuditError, match="ZIP_EXTRA_UNREVIEWED"):
            reviewer._verify_zip_extras(*args)


def test_complete_synthetic_review_cannot_expand_staging_scope(reviewer, tmp_path, monkeypatch):
    prepare = importlib.import_module("prepare_android")
    root = tmp_path / "project"
    monkeypatch.setattr(reviewer, "ROOT", root)
    monkeypatch.setattr(reviewer, "shared_report_revision", lambda _root: "0" * 16)
    override_font_hashes(reviewer, monkeypatch)
    artificial_license_hash = hashlib.sha256(b"synthetic license text").hexdigest()
    monkeypatch.setattr(
        reviewer,
        "FONT_LICENSE_HASHES",
        {name: artificial_license_hash for name in reviewer.FONT_LICENSE_HASHES},
    )
    java_src = root / "android/app/src/main/java/io/example"
    java_src.mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "tests").mkdir()
    (java_src / "Test.java").write_text("// synthetic Java source for audit only\n" * 5)
    source_names = ["fgo_local_exporter/" + name + ".py" for name in reviewer.MODULES] + [
        "fgo_stat_core/" + name for name in reviewer.CORE_MODULES
    ]
    data_names = ["fgo_local_exporter/" + name for name in reviewer.DATA] + [
        "fgo_stat_core/" + name for name in reviewer.CORE_DATA
    ]
    prepared = tmp_path / "prepared"
    for name in source_names + data_names:
        source = root / "src" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        raw = (
            ("# artificial source for " + name + "\n").encode()
            if name.endswith(".py")
            else ("approved runtime " + name).encode()
        )
        if name.endswith("/native_support_ui/asset_hashes.json"):
            raw = native_contracts()[0]
        elif name.endswith("/native_support_ui/font_manifest.json"):
            raw = native_contracts()[1]
        source.write_bytes(raw)
        target = prepared / "python" / (name + "c" if name.endswith(".py") else name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(prepare.compile_release(raw) if name.endswith(".py") else raw)
    licenses = (
        ("LICENSE", "project-MIT.txt"),
        ("licenses/html-to-image-MIT.txt", "html-to-image-MIT.txt"),
        ("licenses/Chaquopy-MIT.txt", "Chaquopy-MIT.txt"),
        ("licenses/Python-3.13.txt", "Python-3.13.txt"),
        ("licenses/OpenSSL-Apache-2.0.txt", "OpenSSL-Apache-2.0.txt"),
        ("licenses/NotoSansCJK-OFL-1.1.txt", "NotoSansCJK-OFL-1.1.txt"),
        ("licenses/DejaVu-Fonts.txt", "DejaVu-Fonts.txt"),
        ("licenses/Font-Notices.txt", "Font-Notices.txt"),
        ("licenses/Android-Dependency-Notices.txt", "Android-Dependency-Notices.txt"),
        ("licenses/Android-Rust-Dependency-Notices.txt", "Android-Rust-Dependency-Notices.txt"),
        ("licenses/MPL-2.0.txt", "MPL-2.0.txt"),
    )
    for source_name, target_name in licenses:
        source = root / source_name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("synthetic license text", encoding="utf8")
        target = prepared / "assets/licenses" / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    profile = {
        "version": "2.129.0",
        "sdk_base": {
            "merchant_id": "80001",
            "game_id": "80002",
            "app_id": "80003",
            "server_id": "80004",
            "version": "80005",
            "sdk_type": "80006",
        },
        "sdk_key": "SyntheticSdkKey123",
        "development_code": "SyntheticDevKey123",
        "session_prefix": "SyntheticPrefix123",
    }
    encoded_profile = json.dumps(profile, separators=(",", ":")).encode()
    (root / "game-protocol.json").write_bytes(encoded_profile)
    (prepared / "assets/game-protocol.json").write_bytes(encoded_profile)
    (prepared / "assets/report-engine.json").write_text(
        json.dumps({"version": 1, "revision": "0" * 16})
    )
    resources = synthetic_public_resources()
    public = {name: hashlib.sha256(value).hexdigest() for name, value in resources.items()}
    (prepared / "assets/public-manifest.json").write_text(json.dumps(public))
    (prepared / "assets/public-resources.zip").write_bytes(synthetic_zip(list(resources.items())))
    app_entries = [
        (path.relative_to(prepared / "python").as_posix(), path.read_bytes())
        for path in (prepared / "python").rglob("*")
        if path.is_file()
    ]
    app_archive = synthetic_zip(app_entries)
    outer = [("classes.dex", b"synthetic dex")]
    outer.extend(
        ("assets/" + path.relative_to(prepared / "assets").as_posix(), path.read_bytes())
        for path in (prepared / "assets").rglob("*")
        if path.is_file()
    )
    outer.append((reviewer.APP_ARCHIVE, app_archive))
    apk, signing = synthetic_signature(synthetic_zip(outer), reviewer)
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    apk_path = delivery / "preview.apk"
    apk_path.write_bytes(apk)
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    (rebuilt / "classes.dex").write_bytes(b"synthetic dex")
    tool_root = tmp_path / "tools"
    tool_root.mkdir()
    tool_files = {name: tool_root / name for name in ("java", "apksigner_jar", "aapt", "zipalign")}
    for name, path in tool_files.items():
        path.write_bytes(("synthetic tool " + name).encode())
    monkeypatch.setattr(reviewer, "verify_dex", lambda _content, _classes: None)
    monkeypatch.setattr(reviewer, "verify_signature", lambda *_args: True)
    monkeypatch.setattr(reviewer, "verify_android_metadata", lambda *_args: True)
    monkeypatch.setattr(reviewer, "verify_inventory", lambda *_args: None)
    monkeypatch.setattr(
        reviewer.subprocess, "run", lambda *_args, **_kwargs: type("Fake", (), {"returncode": 0})()
    )

    def tree_records(root_path):
        result = {}
        for path in root_path.rglob("*"):
            name = path.relative_to(root_path).as_posix()
            result[name + "/" if path.is_dir() else name] = reviewer._record(
                b"" if path.is_dir() else path.read_bytes(),
                "directory" if path.is_dir() else "file",
            )
        return result

    inventory = {}
    authorized = {reviewer.APP_ARCHIVE + "!/" + name for name in reviewer.AUTHORIZED_PAGES}
    profile_secrets = [
        profile[name].encode() for name in ("sdk_key", "development_code", "session_prefix")
    ]
    extra = [b"artificial-signing-secret-7654"]
    reviewer._scan_zip(
        apk,
        "",
        0,
        inventory,
        authorized,
        prepared,
        reviewer._source_maps(),
        {"classes.dex": ["Lsynthetic/Test;"]},
        signing,
        profile_secrets,
        extra,
        reviewer._project_probes(),
        {"size": 0, "count": 0},
    )
    source_hashes = {
        name: reviewer.digest((root / "src" / name).read_bytes())
        for name in source_names + data_names
    }
    manifest = {
        "version": 1,
        "authorized_pages": sorted(reviewer.AUTHORIZED_PAGES),
        "source_hashes": source_hashes,
        "prepared": tree_records(prepared),
        "delivery": tree_records(delivery),
        "inventory": inventory,
        "apk_sha256": reviewer.digest(apk),
        "apk_size": len(apk),
        "signing": signing,
        "certificate_sha256": "a" * 64,
        "android_metadata": {
            "application_id": "io.github.ayamedog.fgostats",
            "version_code": "1",
            "version_name": "synthetic",
            "min_sdk": "24",
            "target_sdk": "35",
            "permissions": [
                "android.permission.INTERNET",
                "android.permission.WRITE_EXTERNAL_STORAGE",
            ],
        },
        "tools": {name: reviewer.digest(path.read_bytes()) for name, path in tool_files.items()},
        "dex_classes": {"classes.dex": ["Lsynthetic/Test;"]},
        "dex_rebuilt": {"classes.dex": reviewer.digest(b"synthetic dex")},
    }
    manifest_path = tmp_path / "independent-manifest.json"
    stage_path = tmp_path / "stage-manifest.json"
    stage_path.write_bytes(b"{}")
    resource_path = tmp_path / "resource-review.json"
    resource_path.write_bytes(json.dumps(public).encode())

    def write_manifest():
        raw = json.dumps(manifest, sort_keys=True).encode()
        manifest_path.write_bytes(raw)
        return manifest_path, reviewer.digest(raw)

    arguments = dict(
        approved_pages=True,
        trusted=write_manifest(),
        secret_values=extra,
        java=tool_files["java"],
        apksigner_jar=tool_files["apksigner_jar"],
        aapt=tool_files["aapt"],
        zipalign=tool_files["zipalign"],
        dex_rebuilt_dir=rebuilt,
        stage_review=(stage_path, reviewer.digest(stage_path.read_bytes())),
        resource_review=(resource_path, reviewer.digest(resource_path.read_bytes())),
    )
    result = reviewer.review(apk_path, prepared, **arguments)
    assert result["apk_sha256"] == reviewer.digest(apk)
    for name in (
        "Android-Dependency-Notices.txt",
        "Android-Rust-Dependency-Notices.txt",
        "MPL-2.0.txt",
    ):
        license_path = prepared / "assets/licenses" / name
        original = license_path.read_bytes()
        license_path.unlink()
        with pytest.raises(reviewer.AuditError, match="STAGE_SCOPE_INVALID"):
            reviewer._verify_prepared(prepared, source_hashes)
        license_path.write_bytes(b"changed synthetic license")
        with pytest.raises(reviewer.AuditError, match="LICENSE_MISMATCH"):
            reviewer._verify_prepared(prepared, source_hashes)
        license_path.write_bytes(original)
    (prepared / "unlisted.txt").write_text("synthetic extra")
    manifest["prepared"] = tree_records(prepared)
    arguments["trusted"] = write_manifest()
    with pytest.raises(RuntimeError, match="STAGE_SCOPE_INVALID"):
        reviewer.review(apk_path, prepared, **arguments)
