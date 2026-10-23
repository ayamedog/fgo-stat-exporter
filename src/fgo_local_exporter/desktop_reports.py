"""同一统计引擎的桌面装配；模板与统计公式不另建副本。"""

import hashlib
import json
import sys
import threading
from pathlib import Path

from fgo_local_exporter.reports import ReportBuilder, shared_report_revision

__all__ = ["DesktopReportBuilder", "engine_source"]


def engine_source():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[1]


class DesktopReportBuilder(ReportBuilder):
    def __init__(self, config):
        self.config = config
        self._master = {}
        self._master_lock = threading.Lock()
        self._revision = self.revision()

    def revision(self):
        digest = hashlib.sha256()
        if getattr(sys, "frozen", False):
            source = json.loads((engine_source() / "engine-manifest.json").read_bytes())["revision"]
        else:
            source = shared_report_revision(self.config.engine_source)
        digest.update(source.encode())
        for directory in (self.config.data_dir, self.config.atlas_dir, self.config.atlas_jp_dir):
            if directory is None:
                continue
            digest.update((directory / "SOURCE_MANIFEST.json").read_bytes())
        return digest.hexdigest()[:16]
