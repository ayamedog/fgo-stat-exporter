"""公开图片下载的地址限制、错误处理和缓存行为。"""

import asyncio

import httpx
import pytest

from fgo_stat_core.contracts.models import CaptureError
from fgo_stat_core.infrastructure.public_download import PublicImageDownloader


@pytest.mark.parametrize(
    "url",
    [
        "https://unapproved.invalid/CN/Items/a.png",
        "https://static.atlasacademy.io/CN/Items/a.png?account=synthetic",
        "https://static.atlasacademy.io/CN/Items/../a.png",
        "http://static.atlasacademy.io/CN/Items/a.png",
    ],
)
def test_only_plain_public_image_urls_are_accepted(url):
    with pytest.raises(CaptureError, match="REPORT_IMAGE_SOURCE_REJECTED"):
        asyncio.run(PublicImageDownloader().fetch(url))


@pytest.mark.parametrize(
    "status,body,limit,expected",
    [
        (200, b"\x89PNG\r\n\x1a\nsynthetic-image", 32, None),
        (302, b"", 32, "REPORT_IMAGE_DOWNLOAD_FAILED"),
        (200, b"larger-than-limit", 4, "REPORT_IMAGE_TOO_LARGE"),
    ],
)
def test_download_is_bounded_and_does_not_follow_redirects(
    monkeypatch,
    status,
    body,
    limit,
    expected,
):
    import fgo_stat_core.infrastructure.public_download as module

    original = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, content=body, headers={"Location": "https://other.invalid/"})

    def client(**options):
        assert options["trust_env"] is False
        assert options["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), **options)

    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    task = PublicImageDownloader(limit=limit).fetch("https://static.atlasacademy.io/CN/Items/a.png")
    if expected:
        with pytest.raises(CaptureError, match=expected):
            asyncio.run(task)
    else:
        assert asyncio.run(task) == body
    assert len(requests) == (2 if expected else 1)
    assert {request.url.host for request in requests} <= {
        "static.atlasacademy.io",
        "static.atlas.chaldea.center",
    }
    assert all("Cookie" not in request.headers for request in requests)
    assert all("Authorization" not in request.headers for request in requests)


def test_domestic_fallback_reuses_connection_without_cookies(monkeypatch):
    import fgo_stat_core.infrastructure.public_download as module

    original = httpx.AsyncClient
    requests, clients = [], []
    payload = b"\x89PNG\r\n\x1a\nsynthetic-image"

    async def handle(request):
        requests.append(request)
        if request.url.host == "static.atlasacademy.io":
            raise httpx.ConnectTimeout("artificial network error")
        return httpx.Response(200, content=payload, headers={"Set-Cookie": "artificial=private"})

    def client(**options):
        clients.append(True)
        return original(transport=httpx.MockTransport(handle), **options)

    monkeypatch.setattr(module.httpx, "AsyncClient", client)

    async def run():
        async with PublicImageDownloader(timeout=4) as downloader:
            assert (
                await downloader.fetch("https://static.atlasacademy.io/CN/Items/a.png") == payload
            )
            assert (
                await downloader.fetch("https://static.atlasacademy.io/CN/Items/b.png") == payload
            )

    asyncio.run(run())
    assert len(clients) == 1 and len(requests) == 2
    assert requests[-1].url.host == "static.atlas.chaldea.center"
    assert all("Cookie" not in request.headers for request in requests)


def test_report_batch_timeout_cancels_downloads_and_keeps_statistics_available(
    monkeypatch, tmp_path
):
    import threading

    from fgo_local_exporter.reports import _prepare_images
    from fgo_stat_core.infrastructure.report_assets import AssetSpec

    active = set()

    async def blocked(_self, url):
        active.add(url)
        try:
            await asyncio.sleep(100)
        finally:
            active.discard(url)

    monkeypatch.setattr(PublicImageDownloader, "fetch", blocked)
    specs = tuple(
        AssetSpec(f"image-{i}.png", f"https://static.atlasacademy.io/CN/Items/{i}.png")
        for i in range(12)
    )
    result = asyncio.run(
        _prepare_images(specs, tmp_path, threading.Event(), lambda *_: None, timeout=0.03)
    )
    assert result == {"available": 0, "missing": 12, "downloaded": 0}
    assert not active
    assert not list(tmp_path.iterdir())


def test_clearing_report_cancels_active_image_downloads(monkeypatch, tmp_path):
    import threading

    from fgo_local_exporter.errors import ExportError
    from fgo_local_exporter.reports import _prepare_images
    from fgo_stat_core.infrastructure.report_assets import AssetSpec

    cancel = threading.Event()
    active = []

    async def blocked(_self, _url):
        active.append(True)
        cancel.set()
        try:
            await asyncio.sleep(100)
        finally:
            active.clear()

    monkeypatch.setattr(PublicImageDownloader, "fetch", blocked)
    specs = (AssetSpec("image-1.png", "https://static.atlasacademy.io/CN/Items/1.png"),)
    with pytest.raises(ExportError, match="CANCELLED"):
        asyncio.run(_prepare_images(specs, tmp_path, cancel, lambda *_: None, timeout=1))
    assert not active


def test_image_progress_counts_existing_and_downloaded_images(monkeypatch, tmp_path):
    import hashlib
    import io
    import threading

    from PIL import Image

    from fgo_local_exporter.reports import _prepare_images
    from fgo_stat_core.infrastructure.report_assets import AssetSpec

    output = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, "PNG")
    image = output.getvalue()
    specs = tuple(
        AssetSpec(f"image-{i}.png", f"https://static.atlasacademy.io/CN/Items/{i}.png")
        for i in range(2)
    )
    (tmp_path / "image-0.png").write_bytes(image)
    (tmp_path / "image-0.png.sha256").write_text(
        hashlib.sha256(specs[0].url.encode()).hexdigest() + ":" + hashlib.sha256(image).hexdigest()
    )
    requests, progress = [], []

    async def fetch(_self, url):
        requests.append(url)
        return image

    monkeypatch.setattr(PublicImageDownloader, "fetch", fetch)
    result = asyncio.run(
        _prepare_images(specs, tmp_path, threading.Event(), lambda *value: progress.append(value))
    )
    assert requests == [specs[1].url]
    assert result == {"available": 2, "missing": 0, "downloaded": 1}
    assert progress == [("images", 1, 2), ("images", 2, 2)]


def test_each_image_can_switch_source_without_waiting_for_another_image(monkeypatch):
    events = []
    image = b"\x89PNG\r\n\x1a\nartificial"

    async def read(_self, url):
        events.append(url)
        if url.endswith("/slow.png"):
            await asyncio.sleep(10)
        return image

    monkeypatch.setattr(PublicImageDownloader, "_read", read)

    async def run():
        source = PublicImageDownloader(timeout=0.05)
        slow = asyncio.create_task(source.fetch("https://static.atlasacademy.io/CN/Items/slow.png"))
        await asyncio.sleep(0)
        fast = await source.fetch("https://static.atlasacademy.io/CN/Items/fast.png")
        assert fast == image
        with pytest.raises(CaptureError):
            await slow

    asyncio.run(run())
    assert any(url.endswith("/fast.png") for url in events)


@pytest.mark.parametrize("broken", ["stamp", "image", "url", "interrupted"])
def test_repair_cache_downloads_again_instead_of_permanent_failure(tmp_path, broken):
    import hashlib
    import io

    from PIL import Image

    from fgo_stat_core.infrastructure.report_assets import AssetSpec, fetch_report_assets

    output = io.BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 0)).save(output, "PNG")
    image = output.getvalue()
    spec = AssetSpec("narrow-990001.png", "https://static.atlasacademy.io/CN/Narrow/990001.png")
    marker = hashlib.sha256(spec.url.encode()).hexdigest() + ":" + hashlib.sha256(image).hexdigest()
    (tmp_path / spec.filename).write_bytes(b"broken" if broken == "image" else image)
    if broken != "interrupted":
        saved = "wrong" if broken == "stamp" else marker
        if broken == "url":
            saved = "0" * 64 + marker[64:]
        (tmp_path / (spec.filename + ".sha256")).write_text(saved)
    calls = []

    class Downloader:
        async def fetch(self, url):
            calls.append(url)
            return image

    result = asyncio.run(fetch_report_assets((spec,), tmp_path, Downloader(), repair_cache=True))
    assert result == {"available": 1, "missing": 0, "downloaded": 1}
    assert calls == [spec.url]
    assert (tmp_path / spec.filename).read_bytes() == image
    assert (tmp_path / (spec.filename + ".sha256")).read_text() == marker
    assert {p.name for p in tmp_path.iterdir()} == {spec.filename, spec.filename + ".sha256"}


def test_missing_images_keep_the_fixed_layout_without_external_requests(tmp_path):
    from fgo_local_exporter.reports import _missing_image_tags

    source = '<img class="ring-symbol" style="left:20px" src="assets/class-1.png" alt="职阶">'
    output, count = _missing_image_tags(source, tmp_path)
    assert count == 1 and 'class="image-missing ring-symbol"' in output
    assert 'style="left:20px"' in output and "图片未下载" in output
    assert "src=" not in output and "http" not in output


def test_report_still_contains_statistics_when_public_images_are_unavailable(
    tmp_path, monkeypatch, minimal_capture, encode_capture, hmac_key
):
    import threading
    from types import SimpleNamespace

    from fgo_local_exporter.reports import ReportBuilder
    from fgo_stat_core.contracts.report import Metric, SupportGroup, SupportSlot
    from fgo_stat_core.features.capture_import.parser import parse_capture
    from fgo_stat_core.features.report.calculator import build_report
    from fgo_stat_core.infrastructure import report_assets

    raw = encode_capture(minimal_capture)
    report = build_report(
        parse_capture(raw, hmac_key, report_fields=True), None, generated_at=1800000000
    )
    positions = (
        "all",
        "saber",
        "archer",
        "lancer",
        "rider",
        "caster",
        "assassin",
        "berserker",
        "extra",
    )
    slots = (
        SupportSlot(
            position="all",
            state="ready",
            servant_id=990001,
            name="人工助战",
            level=80,
            np_level=1,
            skill_levels=(1, 1, 1),
        ),
    ) + tuple(SupportSlot(position=position, state="empty") for position in positions[1:])
    report = report.model_copy(
        update={
            "supports": Metric(
                state="ready",
                value=(SupportGroup(kind="normal", index=1, slots=slots),),
                rule_id="artificial",
                source_tables=("userSupportDeck",),
            )
        }
    )
    import fgo_stat_core.features.report.calculator as calculator
    import fgo_stat_core.infrastructure.native_support_render as native

    monkeypatch.setattr(calculator, "build_report", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(
        report_assets,
        "plan_report_assets",
        lambda *_args, **_kwargs: report_assets._plan(report, {}, {}, {}),
    )

    async def offline(_self, _url):
        raise CaptureError("REPORT_IMAGE_DOWNLOAD_FAILED")

    def no_native(*_args, **_kwargs):
        pytest.fail("缺少助战图片时应保留标准布局")

    monkeypatch.setattr(PublicImageDownloader, "fetch", offline)
    monkeypatch.setattr(native, "render_native_blocks", no_native)
    builder = ReportBuilder.__new__(ReportBuilder)
    builder._revision = "artificial"
    builder.revision = lambda: "artificial"
    builder.master = lambda _region="CN": None
    builder.config = SimpleNamespace(
        asset_dir=tmp_path,
        data_dir=tmp_path,
        native_ui_dir=tmp_path,
        header_font=None,
        regular_font=None,
    )
    document = builder.render(raw, {"friend": False, "support": True}, threading.Event())
    assert document.image_warning == "REPORT_IMAGES_PARTIAL"
    assert "人工助战" in document.html and "80" in document.html
    assert document.data["supports"]["value"][0]["slots"][0]["level"] == 80
    assert 'src="assets/' not in document.html and 'src="http' not in document.html
