#!/usr/bin/env python3
"""Build the private Python runtime followed by the native Tauri bundle."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DESKTOP_ROOT.parent


def tauri_command() -> list[str]:
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError("Rust is required to build the desktop shell: https://rustup.rs")
    completed = subprocess.run(
        [cargo, "tauri", "--version"],
        cwd=DESKTOP_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError("Tauri CLI v2 is required; install it with: cargo install tauri-cli --version '^2' --locked")
    return [cargo, "tauri"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles", help="Comma-separated Tauri bundle types, such as app,dmg or deb,appimage")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--skip-sidecar", action="store_true")
    parser.add_argument("--release-updater", action="store_true")
    args = parser.parse_args()

    subprocess.run([sys.executable, str(DESKTOP_ROOT / "scripts" / "check-version.py")], cwd=PROJECT_ROOT, check=True)
    if not args.skip_sidecar:
        subprocess.run([sys.executable, str(DESKTOP_ROOT / "scripts" / "build-sidecar.py")], cwd=PROJECT_ROOT, check=True)

    command = [*tauri_command(), "build"]
    if args.release_updater:
        subprocess.run(
            [sys.executable, str(DESKTOP_ROOT / "scripts" / "prepare-release-config.py")],
            cwd=PROJECT_ROOT,
            check=True,
        )
        command.extend(["--config", "build/tauri.release.conf.json"])
    if args.debug:
        command.append("--debug")
    if args.bundles:
        command.extend(["--bundles", args.bundles])
    subprocess.run(command, cwd=DESKTOP_ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
