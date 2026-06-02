#!/usr/bin/env python3
"""
Unit test for APSClient JSON parsing.

Verifies that APSClient._get parses the response body with json.loads(text)
and therefore tolerates an unexpected content-type such as
application/octet-stream — which aiohttp's Response.json() rejects by default.

This is a self-contained test (stdlib unittest + mocks); it does not require
pytest, network access, or real APsystems credentials.

Usage:
  python test_json_mimetype.py
"""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp

# Load api.py / const.py directly so we never import apsystems_openapi/__init__.py
# (which requires Home Assistant).
_pkg_dir = Path(__file__).resolve().parent.parent / "apsystems_openapi"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_api_mod = _load_module("aps_api", _pkg_dir / "api.py")
APSClient = _api_mod.APSClient
APSRateLimitError = _api_mod.APSRateLimitError


def _make_session(*, status: int, text: str):
    """Build a fake aiohttp session whose GET returns the given status/body.

    The fake response's .json() raises ContentTypeError to prove _get does not
    rely on it; only .text() is used.
    """
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    response.json = AsyncMock(
        side_effect=aiohttp.ContentTypeError(MagicMock(), (), message="bad mimetype")
    )
    response.raise_for_status = MagicMock()

    get_ctx = MagicMock()
    get_ctx.__aenter__ = AsyncMock(return_value=response)
    get_ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=get_ctx)
    return session, response


class JsonMimetypeTest(unittest.TestCase):
    def _client(self, session):
        return APSClient(
            app_id="app",
            app_secret="secret",
            sid="sid",
            base_url="https://example.invalid",
            session=session,
        )

    def test_parses_octet_stream_json(self):
        """Valid JSON with application/octet-stream content-type is parsed."""
        body = '{"code": 0, "data": {"lifetime": 1234.5}}'
        session, response = _make_session(status=200, text=body)
        client = self._client(session)

        result = asyncio.run(client._get("/user/api/v2/systems/summary/sid"))

        self.assertEqual(result, {"code": 0, "data": {"lifetime": 1234.5}})
        # Confirm we used .text() and never depended on .json().
        response.text.assert_awaited_once()
        response.json.assert_not_awaited()

    def test_rate_limit_code_still_raises(self):
        """A non-zero limit code in octet-stream JSON still raises."""
        body = '{"code": 2005, "data": null}'
        session, _ = _make_session(status=200, text=body)
        client = self._client(session)

        with self.assertRaises(APSRateLimitError):
            asyncio.run(client._get("/user/api/v2/systems/summary/sid"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
