#!/usr/bin/env python3
"""Create the secret-dependent Tauri updater overlay used only in releases."""

import json
import os
from pathlib import Path


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = DESKTOP_ROOT / "build" / "tauri.release.conf.json"
DEFAULT_ENDPOINT = "https://github.com/ServiceStack/llms/releases/latest/download/latest.json"


def main() -> int:
    public_key = os.environ.get("TAURI_UPDATER_PUBLIC_KEY", "").strip()
    if not public_key:
        raise RuntimeError("TAURI_UPDATER_PUBLIC_KEY is required for a release build")
    endpoint = os.environ.get("TAURI_UPDATER_ENDPOINT", DEFAULT_ENDPOINT).strip()
    if not endpoint.startswith("https://"):
        raise RuntimeError("TAURI_UPDATER_ENDPOINT must use HTTPS")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            {
                "bundle": {"createUpdaterArtifacts": True},
                "plugins": {"updater": {"pubkey": public_key, "endpoints": [endpoint]}},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Release updater config: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
