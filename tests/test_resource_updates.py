"""用人工字典模拟上游更新，不请求网络、不使用账号数据。"""

import asyncio
import hashlib
import io
import json
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from fgo_local_exporter.desktop_resources import ResourceManager
from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.resource_catalog import PublicResourceClient, discover_catalog
from fgo_stat_core.infrastructure.report_assets import fetch_report_assets, plan_report_assets
from fgo_stat_core.infrastructure.report_master import load_report_master

DATA = "https://raw.githubusercontent.com/chaldea-center/chaldea-data/main/dist/"
INFO = "https://api.atlasacademy.io/export/CN/info.json"
JP_INFO = "https://api.atlasacademy.io/export/JP/info.json"
EXTRA = "https://data-cn.chaldea.center/mstGachaCNExtra.json"
ATLAS = "https://api.atlasacademy.io/repo/CN/master/"
JP_ATLAS = "https://api.atlasacademy.io/repo/JP/master/"
NEW_SERVANT, NEW_CE = 990006, 880007


@pytest.mark.parametrize("radius", [1, 3, 8])
def test_fast_outline_filter_preserves_every_pixel(radius):
    import random

    from PIL import ImageFilter

    from fgo_stat_core.infrastructure.native_support_ui.typography import _expand_alpha

    image = Image.frombytes("L", (160, 32), random.Random(17).randbytes(160 * 32))
    original = image.filter(ImageFilter.MaxFilter(radius * 2 + 1))
    assert _expand_alpha(image, radius).tobytes() == original.tobytes()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


class SyntheticUpstream:
    def __init__(self):
        self.files = {
            "items.json": [],
            "entities.json": [],
            "baseSkills.json": [],
            "classBoards.json": [],
            "grandGraphs.json": [],
            "constData.json": {},
            "mappingPatch.json": {},
        }
        for prefix, count in (
            ("servants", 5),
            ("craftEssences", 6),
            ("wiki.servants", 5),
            ("wars", 5),
        ):
            self.files.update({f"{prefix}.{i}.json": [] for i in range(1, count + 1)})
        self.files["mappingData.1.json"] = {"entity_release": {"CN": []}}
        self.gacha, self.shop, self.extra = [], [], []
        self.timestamp = 1_800_000_001
        self.atlas_hash = "abcdef"
        self.jp_hash = "123abc"
        self.jp_gacha, self.jp_shop = [], []
        self.requests = []
        self.failure = self.bad_hash = self.block = ""
        self.entered, self.release = threading.Event(), threading.Event()
        self.after_file = None

    def version(self):
        return encoded(
            {
                "timestamp": self.timestamp,
                "files": {
                    name: {
                        "filename": name,
                        "size": len(encoded(value)),
                        "hash": hashlib.md5(encoded(value), usedforsecurity=False).hexdigest()[:6],
                    }
                    for name, value in self.files.items()
                },
            }
        )

    def get(self, url, limit, *, progress=None, stop=None):
        self.requests.append(url)
        if url == self.failure:
            raise ExportError("RESOURCE_DOWNLOAD_FAILED")
        if stop is not None and stop.is_set():
            raise ExportError("CANCELLED")
        if url == self.block:
            self.entered.set()
            assert self.release.wait(3)
        if url == DATA + "version.json":
            raw = self.version()
        elif url == INFO:
            raw = encoded({"hash": self.atlas_hash, "dataVer": 1, "timestamp": self.timestamp})
        elif url == JP_INFO:
            raw = encoded({"hash": self.jp_hash, "dataVer": 1, "timestamp": self.timestamp})
        elif url == EXTRA:
            raw = encoded(self.extra)
        elif url == ATLAS + "mstGacha.json":
            raw = encoded(self.gacha)
        elif url == ATLAS + "mstShop.json":
            raw = encoded(self.shop)
        elif url == JP_ATLAS + "mstGacha.json":
            raw = encoded(self.jp_gacha)
        elif url == JP_ATLAS + "mstShop.json":
            raw = encoded(self.jp_shop)
        elif url.startswith(DATA):
            raw = encoded(self.files[url.removeprefix(DATA)])
        else:
            raise AssertionError("出现非预期资源请求")
        if url == self.bad_hash:
            raw = raw.replace(b"990006", b"990009")
        assert len(raw) <= limit
        if progress:
            progress(len(raw))
        if self.after_file and url != DATA + "version.json":
            self.after_file(url)
        return raw

    def add_new_content(self):
        self.timestamp += 1
        self.files["servants.6.json"] = [
            {
                "id": NEW_SERVANT,
                "collectionNo": 6,
                "name": "New Servant",
                "rarity": 5,
                "classId": 1,
                "extraAssets": {
                    "charaGraph": {
                        "ascension": {
                            "1": "https://static.atlasacademy.io/CN/CharaGraph/990006.png"
                        }
                    },
                    "status": {
                        "ascension": {
                            "1": "https://static.atlasacademy.io/CN/CharaFigure/990006.png"
                        }
                    },
                },
            }
        ]
        self.files["craftEssences.7.json"] = [
            {
                "id": NEW_CE,
                "name": "New CE",
                "extraAssets": {
                    "faces": {
                        "equip": {"1": "https://static.atlasacademy.io/CN/EquipFaces/880007.png"}
                    },
                },
            }
        ]
        self.files["wiki.servants.6.json"] = [{"collectionNo": 6, "obtains": ["limited"]}]
        self.files["wars.6.json"] = [
            {
                "spots": [
                    {
                        "quests": [
                            {
                                "id": 990006,
                                "name": "New Quest",
                                "type": "free",
                                "afterClear": "repeatLast",
                            }
                        ]
                    }
                ]
            }
        ]
        self.files["mappingData.1.json"]["entity_release"]["CN"] = [NEW_SERVANT, NEW_CE]
        self.files["mappingPatch.json"] = {
            "svt_names": {"New Servant": {"CN": "测试新增从者"}},
            "ce_names": {"New CE": {"CN": "测试新增礼装"}},
            "summon_names": {"New Gacha": {"CN": "测试新增卡池"}},
        }
        self.extra = [
            {
                "id": 990007,
                "name": "New Gacha",
                "type": 1,
                "openedAt": 1_800_000_000,
                "closedAt": 1_900_000_000,
                "imageId": 990007,
            }
        ]


def checked(manager):
    activated = []
    manager.prepare(activated.append)
    manager._thread.join(3)
    assert not manager._thread.is_alive()
    return activated


@pytest.fixture
def resources(tmp_path):
    manager, upstream = ResourceManager(tmp_path), SyntheticUpstream()
    manager.client = upstream
    assert checked(manager)
    upstream.requests.clear()
    return manager, upstream


def test_update_loads_new_parts_names_gachas_and_required_images(resources):
    manager, upstream = resources
    old = manager.current
    old_bytes = (old / "data/mappingPatch.json").read_bytes()
    upstream.add_new_content()
    assert checked(manager)
    state = manager.state()
    assert state["phase"] == "ready" and state["result"] == "updated"
    assert manager.current != old
    assert (old / "data/mappingPatch.json").read_bytes() == old_bytes
    master = load_report_master(manager.current / "data", manager.current / "atlas-cn")
    assert master.servants[NEW_SERVANT].name == "测试新增从者"
    assert master.craft_essences[NEW_CE].name == "测试新增礼装"
    assert master.gachas[990007].name == "测试新增卡池"
    assert 990006 in master.quests
    assert DATA + "items.json" not in upstream.requests
    assert ATLAS + "mstGacha.json" in upstream.requests
    report = SimpleNamespace(
        metadata=SimpleNamespace(region="CN"),
        favorites=SimpleNamespace(value=[SimpleNamespace(servant_id=NEW_SERVANT, roles=["push"])]),
        supports=SimpleNamespace(
            value=[
                SimpleNamespace(
                    slots=[
                        SimpleNamespace(
                            state="ready",
                            servant_id=NEW_SERVANT,
                            craft_essence=SimpleNamespace(craft_essence_id=NEW_CE),
                        )
                    ]
                )
            ]
        ),
        grand_supports=None,
        gachas=SimpleNamespace(
            value=SimpleNamespace(
                all_time=[SimpleNamespace(banner_image_id=990007)],
                selected_pool_year=[],
            )
        ),
    )
    specs = plan_report_assets(report, manager.current / "data", native_support=True)
    names = {s.filename for s in specs}
    assert {f"hero-{NEW_SERVANT}.png", f"ce-{NEW_CE}.png", "banner-image-990007.png"} <= names
    output = io.BytesIO()
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(output, "PNG")

    class Images:
        async def fetch(self, url):
            return output.getvalue()

    cache = manager.image_cache(old, manager.current)
    counts = asyncio.run(fetch_report_assets(specs, cache, Images()))
    assert counts["downloaded"] == counts["available"] == len(specs)
    assert (cache / f"ce-{NEW_CE}.png").is_file()
    restored = ResourceManager(manager.root)
    assert restored.restore_active() == manager.current
    assert restored.version == manager.version


def test_unchanged_check_reuses_all_files_without_replacing_active_version(resources):
    manager, upstream = resources
    old = manager.current
    assert checked(manager) == []
    assert manager.current == old
    assert manager.state()["result"] == "current"
    assert manager.state()["repaired"] == 0
    assert set(upstream.requests) == {DATA + "version.json", INFO, JP_INFO, EXTRA}


def test_jp_only_update_keeps_cn_dictionary_separate(resources):
    manager, upstream = resources
    old = manager.current
    cn_gacha = (old / "atlas-cn/mstGacha.json").read_bytes()
    upstream.jp_hash = "654abc"
    upstream.jp_gacha = [{
        "id": 990008, "name": "人工日服卡池", "type": 1,
        "openedAt": 1800000000, "closedAt": 1800600000, "imageId": 998,
    }]
    upstream.jp_shop = [{"id": 401, "shopType": 7}]
    assert checked(manager)
    assert manager.current != old
    assert (manager.current / "atlas-cn/mstGacha.json").read_bytes() == cn_gacha
    jp = load_report_master(manager.current / "data", manager.current / "atlas-jp", region="JP")
    cn = load_report_master(manager.current / "data", manager.current / "atlas-cn")
    assert jp.gachas[990008].name == "人工日服卡池" and jp.shop_types[401] == 7
    assert 990008 not in cn.gachas
    assert JP_ATLAS + "mstGacha.json" in upstream.requests
    assert ATLAS + "mstGacha.json" not in upstream.requests


def test_failed_jp_download_preserves_both_previous_dictionaries(resources):
    manager, upstream = resources
    old = manager.current
    old_cn = (old / "atlas-cn/SOURCE_MANIFEST.json").read_bytes()
    old_jp = (old / "atlas-jp/SOURCE_MANIFEST.json").read_bytes()
    upstream.jp_hash = "654abc"
    upstream.failure = JP_ATLAS + "mstGacha.json"
    assert checked(manager) == []
    assert manager.current == old and manager.state()["ready"]
    assert (old / "atlas-cn/SOURCE_MANIFEST.json").read_bytes() == old_cn
    assert (old / "atlas-jp/SOURCE_MANIFEST.json").read_bytes() == old_jp


def test_bundled_dictionary_is_cached_in_selected_directory_without_network(resources, tmp_path):
    bundle, upstream = resources
    (bundle.current / "assets").mkdir()
    output = io.BytesIO()
    Image.new("RGB", (8, 8), (240, 240, 240)).save(output, "PNG")
    image = output.getvalue()
    (bundle.current / "assets/hero-990006.png").write_bytes(image)
    (bundle.current / "SEED_IMAGES_MANIFEST.json").write_bytes(
        encoded({"hero-990006.png": hashlib.sha256(image).hexdigest()})
    )
    target = ResourceManager(tmp_path / "chosen-data/resources")

    class Offline:
        def get(self, *_args, **_kwargs):
            pytest.fail("复制内置字典不应发起网络请求")

    target.client = Offline()
    active = target.cache_existing(bundle.current)
    assert active.parent == target.root and active != bundle.current
    assert target.state()["ready"]
    for group in ("data", "atlas-cn"):
        assert (active / group / "SOURCE_MANIFEST.json").read_bytes() == (
            bundle.current / group / "SOURCE_MANIFEST.json"
        ).read_bytes()
        for spec in target.manifest["groups"][group]["files"]:
            assert (active / group / spec["filename"]).read_bytes() == (
                bundle.current / group / spec["filename"]
            ).read_bytes()
    assert ResourceManager(target.root).restore_active() == active
    assert target.cache_existing(active) == active
    cache = target.image_cache(bundle.current, active)
    assert cache.is_relative_to(target.root)
    assert (cache / "hero-990006.png").read_bytes() == image
    assert upstream.requests == []


def test_new_bundle_replaces_old_active_dictionary_without_network(resources, tmp_path):
    source, upstream = resources
    old_bundle = source.current
    chosen = ResourceManager(tmp_path / "upgrade/resources")
    old_active = chosen.cache_existing(old_bundle)
    upstream.add_new_content()
    assert checked(source)
    new_bundle = source.current

    class Offline:
        def get(self, *_args, **_kwargs):
            pytest.fail("启用新版内置字典不应等待网络")

    chosen.client = Offline()
    fresh = chosen.restore_for_startup(new_bundle)
    assert fresh != old_active and fresh.parent == chosen.root
    master = load_report_master(fresh / "data", fresh / "atlas-cn")
    assert master.servants[NEW_SERVANT].released
    assert NEW_CE in master.craft_essences and 990007 in master.gachas
    assert chosen.restore_for_startup(old_bundle) == fresh


def test_dictionary_update_keeps_reviewed_images_and_existing_downloads(resources, tmp_path):
    source, upstream = resources
    bundle = source.current
    image = b"artificial-reviewed-resource"
    (bundle / "assets").mkdir()
    (bundle / "assets/hero-990001.png").write_bytes(image)
    (bundle / "SEED_IMAGES_MANIFEST.json").write_bytes(
        encoded({"hero-990001.png": hashlib.sha256(image).hexdigest()})
    )
    chosen = ResourceManager(tmp_path / "phone/resources")
    cache = chosen.image_cache(bundle, bundle)
    (cache / "narrow-990002.png").write_bytes(b"artificial-previous-download")
    upstream.add_new_content()
    assert checked(source)
    updated = chosen.image_cache(bundle, source.current)
    assert updated == cache
    assert (updated / "hero-990001.png").read_bytes() == image
    assert (updated / "narrow-990002.png").read_bytes() == b"artificial-previous-download"


def test_mobile_startup_uses_new_bundle_when_cached_dictionary_is_old(resources, tmp_path):
    source, upstream = resources
    chosen = ResourceManager(tmp_path / "phone/resources")
    chosen.cache_existing(source.current)
    upstream.add_new_content()
    assert checked(source)
    fresh = chosen.restore_for_startup(source.current, cache_bundle=False)
    assert fresh == source.current
    assert load_report_master(fresh / "data", fresh / "atlas-cn").servants[NEW_SERVANT].released


@pytest.mark.parametrize("failure", ["network", "hash", "format", "race", "cancel"])
def test_failed_update_preserves_active_files_pointer_and_builder(resources, failure):
    manager, upstream = resources
    old = manager.current
    pointer = (manager.root / "active.json").read_bytes()
    previous = (old / "data/mappingPatch.json").read_bytes()
    upstream.add_new_content()
    if failure == "network":
        upstream.failure = DATA + "servants.6.json"
    elif failure == "hash":
        upstream.bad_hash = DATA + "servants.6.json"
    elif failure == "format":
        upstream.files["servants.6.json"] = [{"missingId": True}]
    elif failure == "race":
        upstream.after_file = lambda url: setattr(upstream, "timestamp", upstream.timestamp + 1)
    else:
        manager.stop.set()
    assert checked(manager) == []
    assert manager.current == old and manager.state()["ready"]
    assert manager.state()["phase"] == "error"
    assert manager.state()["last_checked_at"] == 0
    assert (manager.root / "active.json").read_bytes() == pointer
    assert (old / "data/mappingPatch.json").read_bytes() == previous
    assert not list(manager.root.glob(".staging-*"))


def test_download_progress_remains_visible_and_concurrent_check_is_rejected(resources):
    manager, upstream = resources
    upstream.add_new_content()
    upstream.block = DATA + "servants.6.json"
    manager.prepare(lambda path: None)
    try:
        assert upstream.entered.wait(3)
        state = manager.state()
        assert state["ready"] and state["phase"] == "downloading"
        assert state["current_file"] == "servants.6.json"
        assert 0 < state["completed"] < state["total"]
        with pytest.raises(ExportError, match="OPERATION_NOT_READY"):
            manager.prepare(lambda path: None)
    finally:
        upstream.release.set()
        manager._thread.join(3)
    assert manager.state()["result"] == "updated"


def test_corrupted_active_file_is_repaired_in_new_directory(resources):
    manager, upstream = resources
    old = manager.current
    (old / "data/items.json").write_bytes(b"broken")
    assert checked(manager)
    assert manager.current != old
    assert (manager.current / "data/items.json").read_bytes() == b"[]"
    assert upstream.requests.count(DATA + "items.json") == 1


def test_incomplete_new_parts_are_rejected_before_any_switch(resources):
    manager, upstream = resources
    upstream.files["servants.7.json"] = []
    assert checked(manager) == []
    assert manager.state()["error"] == "RESOURCE_CATALOG_INCOMPLETE"


def test_failed_builder_switch_rolls_back_without_replacing_active_pointer(resources):
    manager, upstream = resources
    old, calls = manager.current, []
    pointer = (manager.root / "active.json").read_bytes()
    upstream.add_new_content()

    def activate(path):
        calls.append(path)
        if path != old:
            raise ExportError("RESOURCE_FORMAT_UNSUPPORTED")

    manager.prepare(activate)
    manager._thread.join(3)
    assert calls[-1] == old
    assert manager.current == old
    assert manager.state()["phase"] == "error"
    assert (manager.root / "active.json").read_bytes() == pointer


def test_restart_rejects_an_active_pointer_outside_resource_directory(resources):
    manager, _ = resources
    (manager.root / "active.json").write_bytes(encoded({"directory": "../private"}))
    assert ResourceManager(manager.root).restore_active() is None


def test_public_download_bounds_and_redirects_are_enforced(monkeypatch):
    class Client:
        def open(self, request, **kwargs):
            return io.BytesIO(b"x" * 5)

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Client())
    with pytest.raises(ExportError, match="RESOURCE_SIZE_LIMIT"):
        PublicResourceClient().get(DATA + "items.json", 4)


def test_catalog_ignores_arbitrary_urls_in_upstream_data():
    upstream = SyntheticUpstream()
    upstream.files["unneeded.json"] = {"url": "http://127.0.0.1/private"}
    catalog = discover_catalog(upstream, threading.Event())
    assert all(
        f["filename"] != "unneeded.json"
        for group in catalog.manifest["groups"].values()
        for f in group["files"]
    )
