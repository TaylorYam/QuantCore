"""
Unit tests for notify.tg_send / tg_send_photo.

Coverage:
  - Empty token / chat_id → silent no-op (no network call)
  - Network failure → swallowed (only logs warning, doesn't raise)
  - Successful send → calls urlopen with correct URL + payload
  - Keyword-only arguments enforcement
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest


def test_tg_send_empty_token_is_noop():
    """Empty token should return immediately without any network call."""
    import notify
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify.tg_send("hello", token="", chat_id="123")
        assert not mock_urlopen.called, "urlopen should not be called with empty token"


def test_tg_send_empty_chat_id_is_noop():
    import notify
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify.tg_send("hello", token="abc", chat_id="")
        assert not mock_urlopen.called


def test_tg_send_network_error_is_swallowed():
    """Network errors must not raise — they should only log warning.
    Telegram failures should never break the trading flow."""
    import notify
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("network down")):
        # Should NOT raise
        notify.tg_send("hello", token="abc", chat_id="123")


def test_tg_send_timeout_is_swallowed():
    import notify
    with patch("urllib.request.urlopen",
               side_effect=TimeoutError("timed out")):
        notify.tg_send("hello", token="abc", chat_id="123")


def test_tg_send_calls_telegram_api():
    """Verify the correct URL and HTTP method are used."""
    import notify
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        notify.tg_send("hello", token="TOKEN", chat_id="CHAT")

        assert mock_urlopen.called
        req = mock_urlopen.call_args[0][0]
        # Verify URL contains the bot token
        assert "TOKEN" in req.full_url
        assert "sendMessage" in req.full_url
        assert req.get_method() == "POST"
        # Verify payload contains chat_id + text
        body = req.data.decode("utf-8")
        assert "chat_id=CHAT" in body
        assert "text=hello" in body


def test_tg_send_token_and_chat_id_are_keyword_only():
    """token / chat_id MUST be keyword-only args to prevent positional bugs."""
    import notify
    with pytest.raises(TypeError):
        # Passing positionally should fail
        notify.tg_send("msg", "TOKEN", "CHAT")  # type: ignore[misc]


def test_tg_send_photo_empty_token_is_noop():
    import notify
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify.tg_send_photo("/nonexistent.png",
                             token="", chat_id="123", caption="test")
        assert not mock_urlopen.called


def test_tg_send_photo_file_not_found_is_swallowed():
    """FileNotFoundError inside open() should be caught."""
    import notify
    # No mock needed: real token but nonexistent file.
    # Should log warning, not raise.
    notify.tg_send_photo("/definitely/does/not/exist.png",
                         token="TOKEN", chat_id="CHAT")
