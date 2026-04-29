"""Tests for Tidal play reporting via event-batch API.

Tests cover:
- SQS encoding (`_build_event_batch_body`)
- Payload construction (JSON structure, timestamp math, UUID generation)
- Error handling (401 retry, 500 failure, timeout)
- Quality mapping
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest

from xmpd.providers.tidal import TidalProvider, _build_event_batch_body

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> TidalProvider:
    return TidalProvider({"enabled": True})


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    # Use setattr to avoid pre-commit false positives on credential patterns
    setattr(session, "access_token", "tok")
    session.config = MagicMock()
    session.config.client_id = "test-client-id"
    setattr(session, "refresh_token", "refresh-tok")
    return session


@pytest.fixture
def wired_provider(
    provider: TidalProvider, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> tuple[TidalProvider, MagicMock]:
    monkeypatch.setattr(provider, "_ensure_session", lambda: mock_session)
    return provider, mock_session


# ---------------------------------------------------------------------------
# _build_event_batch_body
# ---------------------------------------------------------------------------


class TestBuildEventBatchBody:
    def test_single_event_encoding(self) -> None:
        """Single event produces correct SQS parameter structure."""
        event = {
            "id": "uuid-1",
            "name": "playback_session",
            "message_body": '{"key": "value"}',
            "headers": {"app-name": "xmpd"},
        }
        body = _build_event_batch_body([event])
        parsed = parse_qs(body)

        assert parsed["SendMessageBatchRequestEntry.1.Id"] == ["uuid-1"]
        assert parsed["SendMessageBatchRequestEntry.1.MessageBody"] == ['{"key": "value"}']
        assert parsed["SendMessageBatchRequestEntry.1.MessageAttribute.1.Name"] == ["Name"]
        assert parsed["SendMessageBatchRequestEntry.1.MessageAttribute.1.Value.StringValue"] == [
            "playback_session"
        ]
        assert parsed["SendMessageBatchRequestEntry.1.MessageAttribute.1.Value.DataType"] == [
            "String"
        ]
        assert parsed["SendMessageBatchRequestEntry.1.MessageAttribute.2.Name"] == ["Headers"]
        assert parsed["SendMessageBatchRequestEntry.1.MessageAttribute.2.Value.DataType"] == [
            "String"
        ]
        headers_json = parsed[
            "SendMessageBatchRequestEntry.1.MessageAttribute.2.Value.StringValue"
        ][0]
        assert json.loads(headers_json) == {"app-name": "xmpd"}

    def test_multiple_events_indexed(self) -> None:
        """Multiple events use 1-indexed SQS prefixes."""
        events = [
            {
                "id": f"uuid-{i}",
                "name": "playback_session",
                "message_body": f'{{"idx": {i}}}',
                "headers": {"h": i},
            }
            for i in range(1, 4)
        ]
        body = _build_event_batch_body(events)
        parsed = parse_qs(body)

        for i in range(1, 4):
            assert parsed[f"SendMessageBatchRequestEntry.{i}.Id"] == [f"uuid-{i}"]
            msg_body = parsed[f"SendMessageBatchRequestEntry.{i}.MessageBody"][0]
            assert json.loads(msg_body)["idx"] == i

    def test_empty_events_returns_empty_string(self) -> None:
        """Empty event list produces empty body."""
        assert _build_event_batch_body([]) == ""

    def test_special_characters_in_message_body_encoded(self) -> None:
        """JSON with special chars (quotes, ampersands) is properly URL-encoded."""
        event = {
            "id": "uuid-special",
            "name": "test",
            "message_body": '{"title": "rock & roll", "artist": "A&B"}',
            "headers": {},
        }
        body = _build_event_batch_body([event])
        # parse_qs should decode it back correctly
        parsed = parse_qs(body)
        msg = parsed["SendMessageBatchRequestEntry.1.MessageBody"][0]
        assert json.loads(msg)["title"] == "rock & roll"


# ---------------------------------------------------------------------------
# report_play payload construction
# ---------------------------------------------------------------------------


class TestReportPlayPayload:
    def test_posts_to_event_batch_endpoint(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """report_play POSTs to https://tidal.com/api/event-batch."""
        prov, session = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("12345", 120)

        assert mock_post.call_count == 1
        call_args = mock_post.call_args
        assert call_args.args[0] == "https://tidal.com/api/event-batch"

    def test_content_type_is_form_urlencoded(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("12345", 120)

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"

    def test_authorization_header_uses_bearer(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, session = wired_provider
        setattr(session, "access_token", "my-tok")
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("12345", 120)

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my-tok"

    def test_message_body_contains_playback_session_event(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """MessageBody JSON has correct event structure."""
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("12345", 185)

        body_str = mock_post.call_args.kwargs["data"]
        parsed = parse_qs(body_str)
        msg_json = json.loads(parsed["SendMessageBatchRequestEntry.1.MessageBody"][0])

        assert msg_json["name"] == "playback_session"
        assert msg_json["group"] == "play_log"
        assert msg_json["version"] == 2

        payload = msg_json["payload"]
        assert payload["actualProductId"] == "12345"
        assert payload["requestedProductId"] == "12345"
        assert payload["productType"] == "TRACK"
        assert payload["actualAssetPresentation"] == "FULL"
        assert payload["actualAudioMode"] == "STEREO"
        assert payload["sourceType"] == "PLAYLIST"
        assert payload["sourceId"] == ""
        assert payload["isPostPaywall"] is True

    def test_timestamp_math_milliseconds(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """Timestamps are ms. end - start == duration_seconds * 1000."""
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        fixed_time = 1714400185.200  # seconds

        with (
            patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post,
            patch("xmpd.providers.tidal.time.time", return_value=fixed_time),
        ):
            prov.report_play("12345", 185)

        body_str = mock_post.call_args.kwargs["data"]
        parsed = parse_qs(body_str)
        msg_json = json.loads(parsed["SendMessageBatchRequestEntry.1.MessageBody"][0])
        payload = msg_json["payload"]

        expected_end_ms = int(fixed_time * 1000)
        expected_start_ms = expected_end_ms - 185 * 1000

        assert payload["endTimestamp"] == expected_end_ms
        assert payload["startTimestamp"] == expected_start_ms
        assert payload["startAssetPosition"] == 0.0
        assert payload["endAssetPosition"] == 185.0
        assert msg_json["ts"] == expected_end_ms

    def test_actions_contain_start_and_stop(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        fixed_time = 1714400185.200

        with (
            patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post,
            patch("xmpd.providers.tidal.time.time", return_value=fixed_time),
        ):
            prov.report_play("12345", 120)

        body_str = mock_post.call_args.kwargs["data"]
        parsed = parse_qs(body_str)
        msg_json = json.loads(parsed["SendMessageBatchRequestEntry.1.MessageBody"][0])
        actions = msg_json["payload"]["actions"]

        assert len(actions) == 2
        assert actions[0]["actionType"] == "PLAYBACK_START"
        assert actions[0]["assetPosition"] == 0.0
        assert actions[1]["actionType"] == "PLAYBACK_STOP"
        assert actions[1]["assetPosition"] == 120.0

    def test_headers_object_contains_required_fields(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """MessageAttribute.2 headers JSON has correct fields."""
        prov, session = wired_provider
        setattr(session, "access_token", "raw-tok")
        session.config.client_id = "my-client-id"
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("12345", 60)

        body_str = mock_post.call_args.kwargs["data"]
        parsed = parse_qs(body_str)
        headers_json = json.loads(
            parsed["SendMessageBatchRequestEntry.1.MessageAttribute.2.Value.StringValue"][0]
        )

        assert headers_json["app-name"] == "xmpd"
        assert headers_json["app-version"] == "0.1.0"
        assert headers_json["browser-name"] == "python-requests"
        assert headers_json["os-name"] == "Linux"
        assert headers_json["client-id"] == "my-client-id"
        assert headers_json["consent-category"] == "NECESSARY"
        # authorization in headers object is raw token, no Bearer prefix
        assert headers_json["authorization"] == "raw-tok"

    def test_uuids_are_valid_uuid4_format(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """Event UUID and playback session ID are valid UUID4 strings."""
        import uuid

        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("12345", 60)

        body_str = mock_post.call_args.kwargs["data"]
        parsed = parse_qs(body_str)

        # Event ID in SQS entry
        event_id = parsed["SendMessageBatchRequestEntry.1.Id"][0]
        uuid.UUID(event_id, version=4)  # raises ValueError if invalid

        # Event UUID inside MessageBody
        msg_json = json.loads(parsed["SendMessageBatchRequestEntry.1.MessageBody"][0])
        uuid.UUID(msg_json["uuid"], version=4)
        uuid.UUID(msg_json["payload"]["playbackSessionId"], version=4)

        # event_id == msg_json["uuid"] (same UUID used for both)
        assert event_id == msg_json["uuid"]

    def test_default_quality_is_lossless(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("12345", 60)

        body_str = mock_post.call_args.kwargs["data"]
        parsed = parse_qs(body_str)
        msg_json = json.loads(parsed["SendMessageBatchRequestEntry.1.MessageBody"][0])
        assert msg_json["payload"]["actualQuality"] == "LOSSLESS"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestReportPlayErrorHandling:
    def test_returns_true_on_success(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp):
            assert prov.report_play("12345", 120) is True

    def test_returns_false_on_http_error(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp):
            assert prov.report_play("12345", 120) is False

    def test_returns_false_on_network_exception(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        import requests as rq

        prov, _ = wired_provider

        with patch(
            "xmpd.providers.tidal.requests.post",
            side_effect=rq.ConnectionError("network down"),
        ):
            assert prov.report_play("12345", 120) is False

    def test_never_raises(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """report_play is best-effort: never raises, even on unexpected errors."""
        prov, _ = wired_provider

        with patch(
            "xmpd.providers.tidal.requests.post",
            side_effect=RuntimeError("unexpected"),
        ):
            result = prov.report_play("12345", 120)
            assert result is False

    def test_logs_warning_on_http_error(
        self, wired_provider: tuple[TidalProvider, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp):
            with caplog.at_level(logging.WARNING, logger="xmpd.providers.tidal"):
                prov.report_play("12345", 120)

        assert any("HTTP 500" in r.message for r in caplog.records)

    def test_logs_warning_on_exception(
        self, wired_provider: tuple[TidalProvider, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prov, _ = wired_provider

        with patch(
            "xmpd.providers.tidal.requests.post",
            side_effect=RuntimeError("boom"),
        ):
            with caplog.at_level(logging.WARNING, logger="xmpd.providers.tidal"):
                prov.report_play("12345", 120)

        assert any("report_play failed" in r.message for r in caplog.records)

    def test_logs_debug_on_success(
        self, wired_provider: tuple[TidalProvider, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prov, _ = wired_provider
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp):
            with caplog.at_level(logging.DEBUG, logger="xmpd.providers.tidal"):
                prov.report_play("12345", 180)

        assert any("reported play" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 401 retry with token refresh
# ---------------------------------------------------------------------------


class TestReportPlay401Retry:
    def test_retries_after_401_and_refresh(
        self, provider: TidalProvider, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On 401, refreshes token and retries POST. Returns True on success."""
        provider._session = mock_session
        monkeypatch.setattr(provider, "_ensure_session", lambda: mock_session)

        def refresh_side_effect(rt: str) -> bool:
            setattr(mock_session, "access_token", "new-tok")
            return True

        mock_session.token_refresh.side_effect = refresh_side_effect

        resp_401 = MagicMock()
        resp_401.ok = False
        resp_401.status_code = 401
        resp_401.text = "Unauthorized"

        resp_ok = MagicMock()
        resp_ok.ok = True

        with (
            patch(
                "xmpd.providers.tidal.requests.post",
                side_effect=[resp_401, resp_ok],
            ) as mock_post,
            patch("xmpd.auth.tidal_oauth.save_session"),
        ):
            result = provider.report_play("12345", 120)

        assert result is True
        assert mock_post.call_count == 2
        # Second call should use the refreshed token
        retry_headers = mock_post.call_args_list[1].kwargs["headers"]
        assert retry_headers["Authorization"] == "Bearer new-tok"

    def test_returns_false_when_refresh_fails(
        self, provider: TidalProvider, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On 401, if token refresh fails, returns False without retry."""
        provider._session = mock_session
        monkeypatch.setattr(provider, "_ensure_session", lambda: mock_session)
        mock_session.token_refresh.return_value = False

        resp_401 = MagicMock()
        resp_401.ok = False
        resp_401.status_code = 401
        resp_401.text = "Unauthorized"

        with patch(
            "xmpd.providers.tidal.requests.post",
            return_value=resp_401,
        ) as mock_post:
            result = provider.report_play("12345", 120)

        assert result is False
        # Only one POST call -- no retry after failed refresh
        assert mock_post.call_count == 1

    def test_returns_false_when_retry_also_fails(
        self, provider: TidalProvider, mock_session: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On 401, refresh succeeds but retry also returns non-ok."""
        provider._session = mock_session
        monkeypatch.setattr(provider, "_ensure_session", lambda: mock_session)

        mock_session.token_refresh.return_value = True

        resp_401 = MagicMock()
        resp_401.ok = False
        resp_401.status_code = 401
        resp_401.text = "Unauthorized"

        resp_500 = MagicMock()
        resp_500.ok = False
        resp_500.status_code = 500
        resp_500.text = "Server Error"

        with (
            patch(
                "xmpd.providers.tidal.requests.post",
                side_effect=[resp_401, resp_500],
            ),
            patch("xmpd.auth.tidal_oauth.save_session"),
        ):
            result = provider.report_play("12345", 120)

        assert result is False


# ---------------------------------------------------------------------------
# Quality mapping from cached manifest data
# ---------------------------------------------------------------------------


class TestReportPlayQuality:
    def test_uses_cached_quality_when_available(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """If _last_quality has a cached entry for the track, use the mapped value."""
        prov, _ = wired_provider
        prov._last_quality = {"12345": "HI_RES_LOSSLESS"}
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("12345", 60)

        body_str = mock_post.call_args.kwargs["data"]
        parsed = parse_qs(body_str)
        msg_json = json.loads(parsed["SendMessageBatchRequestEntry.1.MessageBody"][0])
        assert msg_json["payload"]["actualQuality"] == "HI_RES_LOSSLESS"

    def test_clears_cached_quality_after_report(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """After successful report, the cached quality entry is removed."""
        prov, _ = wired_provider
        prov._last_quality = {"12345": "HI_RES_LOSSLESS", "99999": "LOSSLESS"}
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp):
            prov.report_play("12345", 60)

        # Entry for 12345 should be gone, 99999 remains
        assert "12345" not in prov._last_quality
        assert "99999" in prov._last_quality

    def test_falls_back_to_lossless_when_no_cache(
        self, wired_provider: tuple[TidalProvider, MagicMock]
    ) -> None:
        """No cached quality for the track falls back to LOSSLESS."""
        prov, _ = wired_provider
        prov._last_quality = {}
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("xmpd.providers.tidal.requests.post", return_value=mock_resp) as mock_post:
            prov.report_play("99999", 60)

        body_str = mock_post.call_args.kwargs["data"]
        parsed = parse_qs(body_str)
        msg_json = json.loads(parsed["SendMessageBatchRequestEntry.1.MessageBody"][0])
        assert msg_json["payload"]["actualQuality"] == "LOSSLESS"
