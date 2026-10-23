from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from recovery.api.app import browser_phone_enabled
from recovery.config import get_settings
from recovery.voice.dialer import BROWSER_CALL_PREFIX, BrowserDialer, CallRequest, get_dialer


@pytest.fixture
def browser_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DIALER", "browser")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


async def test_browser_dialer_returns_unguessable_placeholder(browser_mode: None) -> None:
    dialer = get_dialer()
    assert isinstance(dialer, BrowserDialer)
    req = CallRequest("recover:s:t", "+971501234567", {})
    a, b = await dialer.dial(req), await dialer.dial(req)
    assert a.startswith(BROWSER_CALL_PREFIX) and a != b
    assert len(a) > 20


def test_phone_is_local_only(browser_mode: None) -> None:
    browser_phone_enabled(_request({}))
    with pytest.raises(HTTPException):
        browser_phone_enabled(_request({"CF-Connecting-IP": "203.0.113.7"}))


def test_phone_is_off_for_other_dialers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIALER", "fake")
    get_settings.cache_clear()
    with pytest.raises(HTTPException):
        browser_phone_enabled(_request({}))
    get_settings.cache_clear()
