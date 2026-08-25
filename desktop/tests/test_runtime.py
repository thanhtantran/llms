import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request


DESKTOP_PYTHON = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(DESKTOP_PYTHON))

import desktop_runtime


TOKEN = "0123456789abcdef0123456789abcdef"
HOST = "127.0.0.1:18000"


def run(coroutine):
    return asyncio.run(coroutine)


class TestDesktopRuntime(unittest.TestCase):
    def setUp(self):
        desktop_runtime._port = 18000
        desktop_runtime._token = TOKEN
        desktop_runtime._version = "test"
        desktop_runtime._stop_event = None

    async def ok_handler(self, request):
        return web.json_response({"ok": True})

    def request(self, path="/", method="GET", headers=None):
        request_headers = {"Host": HOST}
        request_headers.update(headers or {})
        return make_mocked_request(method, path, headers=request_headers)

    def test_health_is_available_before_session_bootstrap(self):
        response = run(
            desktop_runtime.desktop_middleware(
                self.request(desktop_runtime.HEALTH_PATH),
                self.ok_handler,
            )
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_main_application_requires_authenticated_session(self):
        response = run(desktop_runtime.desktop_middleware(self.request(), self.ok_handler))
        self.assertEqual(response.status, 401)

    def test_cookie_authenticates_main_application(self):
        response = run(
            desktop_runtime.desktop_middleware(
                self.request(headers={"Cookie": f"{desktop_runtime.COOKIE}={TOKEN}"}),
                self.ok_handler,
            )
        )
        self.assertEqual(response.status, 200)

    def test_supervisor_header_authenticates_shutdown(self):
        stop_event = asyncio.Event()
        desktop_runtime._stop_event = stop_event
        response = run(
            desktop_runtime.desktop_middleware(
                self.request(
                    desktop_runtime.SHUTDOWN_PATH,
                    method="POST",
                    headers={"X-LLMS-Desktop-Token": TOKEN},
                ),
                self.ok_handler,
            )
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(stop_event.is_set())

    def test_bootstrap_sets_private_session_cookie(self):
        response = run(
            desktop_runtime.desktop_middleware(
                self.request(f"{desktop_runtime.BOOTSTRAP_PREFIX}{TOKEN}"),
                self.ok_handler,
            )
        )
        cookie = response.cookies[desktop_runtime.COOKIE]
        self.assertEqual(response.status, 200)
        self.assertIn('http-equiv="refresh"', response.text)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")

    def test_invalid_bootstrap_token_is_rejected(self):
        response = run(
            desktop_runtime.desktop_middleware(
                self.request(f"{desktop_runtime.BOOTSTRAP_PREFIX}wrong"),
                self.ok_handler,
            )
        )
        self.assertEqual(response.status, 401)

    def test_cross_origin_and_wrong_host_requests_are_rejected(self):
        wrong_origin = run(
            desktop_runtime.desktop_middleware(
                self.request(
                    headers={
                        "Cookie": f"{desktop_runtime.COOKIE}={TOKEN}",
                        "Origin": "https://attacker.example",
                    }
                ),
                self.ok_handler,
            )
        )
        wrong_host = run(
            desktop_runtime.desktop_middleware(
                make_mocked_request("GET", "/", headers={"Host": "localhost:18000"}),
                self.ok_handler,
            )
        )
        self.assertEqual(wrong_origin.status, 403)
        self.assertEqual(wrong_host.status, 403)

    def test_optional_tools_are_reported_without_becoming_requirements(self):
        with patch("desktop_runtime.shutil.which", return_value=None):
            capabilities = desktop_runtime.capabilities()
        self.assertEqual(
            set(capabilities["tools"]),
            {"git", "uv", "ffmpeg", "typst", "dotnet", "bun"},
        )
        self.assertTrue(all(not tool["available"] for tool in capabilities["tools"].values()))

    def test_installation_only_replaces_runner_in_private_process(self):
        llms_main = importlib.import_module("llms.main")

        original = llms_main.web.run_app
        try:
            desktop_runtime.install_desktop_runtime()
            self.assertIs(llms_main.web.run_app, desktop_runtime.desktop_run_app)
            self.assertEqual(desktop_runtime._version, llms_main.VERSION)
        finally:
            llms_main.web.run_app = original

    def test_missing_desktop_token_fails_before_binding(self):
        app = web.Application()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, desktop_runtime.TOKEN_ENV):
                desktop_runtime.desktop_run_app(app, port=18000)


if __name__ == "__main__":
    unittest.main()
