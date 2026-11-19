"""Create the private local game-protocol.json from Actions secrets."""

import argparse
import json
import os
from pathlib import Path

from fgo_local_exporter.game_profile import checked_profile, profile_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("game-protocol.json"))
    args = parser.parse_args()

    secret_names = ("FGO_SDK_KEY", "FGO_DEVELOPMENT_CODE", "FGO_SESSION_PREFIX")
    secrets = {name: os.environ.get(name, "") for name in secret_names}
    if any(not value for value in secrets.values()):
        raise SystemExit("BUILD_SECRET_MISSING")

    profile = checked_profile(
        {
            "version": "2.129.0",
            "sdk_base": {
                "merchant_id": "1",
                "game_id": "112",
                "app_id": "112",
                "server_id": "248",
                "version": "3",
                "sdk_type": "1",
            },
            "sdk_key": secrets["FGO_SDK_KEY"],
            "development_code": secrets["FGO_DEVELOPMENT_CODE"],
            "session_prefix": secrets["FGO_SESSION_PREFIX"],
        }
    )
    output = args.output
    if output.exists() or output.is_symlink():
        raise SystemExit("CONFIG_OUTPUT_EXISTS")
    output.write_text(
        json.dumps(profile_values(profile), separators=(",", ":")),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
