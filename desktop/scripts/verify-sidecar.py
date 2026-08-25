#!/usr/bin/env python3
"""Verify that the frozen sidecar starts without a system Python dependency."""

import argparse
import http.cookiejar
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_ROOT = DESKTOP_ROOT / "src-tauri" / "resources" / "sidecar"
TOKEN = "desktop-sidecar-verification-token-0123456789abcdef"


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_json(url: str, opener=None, headers=None, data=None):
    request = urllib.request.Request(url, headers=headers or {}, data=data)
    response = opener.open(request, timeout=2) if opener else urllib.request.urlopen(request, timeout=2)
    with response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_health(base_url: str, process: subprocess.Popen, timeout: float = 45) -> dict:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate()
            raise RuntimeError(f"Sidecar exited before becoming ready ({process.returncode}):\n{output}")
        try:
            return request_json(f"{base_url}/~desktop/health")
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for sidecar health: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sidecar", type=Path)
    args = parser.parse_args()

    executable_name = "llms-desktop.exe" if os.name == "nt" else "llms-desktop"
    executable = args.sidecar or SIDECAR_ROOT / executable_name
    if not executable.is_file():
        parser.error(f"Sidecar not found: {executable}")

    help_result = subprocess.run([str(executable), "--help"], capture_output=True, text=True, timeout=30)
    if help_result.returncode != 0 or "--serve" not in help_result.stdout:
        raise RuntimeError(f"Frozen sidecar help failed:\n{help_result.stdout}\n{help_result.stderr}")

    port = available_port()
    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="llms-desktop-verify-") as home:
        env = os.environ.copy()
        env.update({"LLMS_HOME": home, "LLMS_DESKTOP_TOKEN": TOKEN, "PYTHONUNBUFFERED": "1"})
        process = subprocess.Popen(
            [str(executable), "--serve", str(port)],
            cwd=home,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            health = wait_for_health(base_url, process)
            if health.get("status") != "ready" or not health.get("desktop"):
                raise RuntimeError(f"Unexpected health response: {health}")

            cookie_jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
            with opener.open(f"{base_url}/~desktop/bootstrap/{TOKEN}", timeout=5) as response:
                bootstrap = response.read().decode("utf-8")
            if 'http-equiv="refresh"' not in bootstrap:
                raise RuntimeError("Desktop bootstrap did not return the same-site handoff page")
            with opener.open(f"{base_url}/", timeout=5) as response:
                html = response.read().decode("utf-8")
            if '<div id="app">' not in html or "<title>llms.py</title>" not in html:
                raise RuntimeError("Authenticated desktop session did not reach the llms-py UI")

            capabilities = request_json(f"{base_url}/~desktop/capabilities", opener=opener)
            if not capabilities.get("runtime", {}).get("frozen"):
                raise RuntimeError(f"Sidecar is not running as a frozen Python application: {capabilities}")

            stopped = request_json(
                f"{base_url}/~desktop/shutdown",
                headers={"X-LLMS-Desktop-Token": TOKEN, "Content-Type": "application/json"},
                data=b"{}",
            )
            if stopped.get("status") != "stopping":
                raise RuntimeError(f"Unexpected shutdown response: {stopped}")
            process.wait(timeout=15)
            output, _ = process.communicate()
            if process.returncode != 0:
                raise RuntimeError(f"Sidecar shutdown failed ({process.returncode}):\n{output}")
            required_extensions = ("providers", "core_tools", "computer")
            broken = [
                name
                for name in required_extensions
                if f"Failed to load extension {name} parser" in output
                or f"Failed to install extension {name}" in output
            ]
            if broken:
                raise RuntimeError(
                    f"Frozen runtime is missing dependencies for required extensions {broken}:\n{output}"
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    print(f"Verified frozen desktop sidecar: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
