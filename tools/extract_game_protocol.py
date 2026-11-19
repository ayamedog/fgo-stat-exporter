"""Extract and validate the app-level protocol profile from a user-supplied APK."""

import argparse
import json
import os
import zipfile
from pathlib import Path

from fgo_local_exporter.game_profile import checked_profile, profile_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("game-protocol.json"))
    args = parser.parse_args()

    apk = args.apk
    output = args.output
    if apk.is_symlink() or not apk.is_file():
        raise SystemExit("APK_INPUT_INVALID")
    if output.exists() or output.is_symlink():
        raise SystemExit("CONFIG_OUTPUT_EXISTS")

    try:
        with zipfile.ZipFile(apk) as archive:
            entries = [item for item in archive.infolist()
                       if item.filename == "assets/game-protocol.json"]
            if len(entries) != 1 or not 1 <= entries[0].file_size <= 16384:
                raise SystemExit("APK_PROTOCOL_NOT_FOUND")
            info = entries[0]
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise SystemExit("APK_PROTOCOL_INVALID")
            raw = archive.read(info)
        profile = checked_profile(json.loads(raw))
    except SystemExit:
        raise
    except (OSError, ValueError, TypeError, zipfile.BadZipFile):
        raise SystemExit("APK_PROTOCOL_INVALID") from None

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(profile_values(profile), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        output.chmod(0o600)
    print("协议配置已提取并通过格式校验；密钥值未输出。")


if __name__ == "__main__":
    main()
