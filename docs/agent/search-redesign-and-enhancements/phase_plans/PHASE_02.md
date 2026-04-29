# Phase 2: Tidal Play Reporting

**Feature**: search-redesign-and-enhancements
**Estimated Context Budget**: ~80k tokens

**Difficulty**: hard
**Visual**: no

**Execution Mode**: parallel
**Batch**: 1

---

## Objective

Replace the no-op `report_play` in `xmpd/providers/tidal.py` (which calls `track.get_stream()` and discards the result) with proper play reporting via Tidal's event-batch API. When a Tidal track crosses the 30-second play threshold, POST a `playback_session` event to `https://tidal.com/api/event-batch` using SQS `SendMessageBatchRequestEntry` encoding.

---

## Deliverables

1. Modified `xmpd/providers/tidal.py` -- new `report_play()` implementation with event-batch POST
2. New `tests/test_tidal_play_report.py` -- tests for payload construction and encoding

---

## Detailed Requirements

### Architecture

The existing flow already works for triggering:
1. `HistoryReporter` monitors MPD, tracks play/pause timing
2. When a Tidal track exceeds 30s of play, it calls `tidal_provider.report_play(track_id, duration_seconds)`
3. The `report_play` interface (`Provider` protocol) stays unchanged: `report_play(track_id: str, duration_seconds: int) -> bool`

What changes: the Tidal provider's `report_play()` body. Instead of calling `track.get_stream()`, it builds an SQS-formatted payload and POSTs it.

### Implementation plan

#### Step 1: Add a helper function for SQS encoding

Add a private function `_build_event_batch_body(events: list[dict]) -> str` to `tidal.py` (or keep it as a module-level function). This encodes events into `application/x-www-form-urlencoded` SQS `SendMessageBatchRequestEntry` format:

```python
def _build_event_batch_body(events: list[dict]) -> str:
    params = []
    for i, event in enumerate(events, start=1):
        prefix = f"SendMessageBatchRequestEntry.{i}"
        attr_prefix = f"{prefix}.MessageAttribute"
        params.append((f"{prefix}.Id", event["id"]))
        params.append((f"{prefix}.MessageBody", event["message_body"]))
        params.append((f"{attr_prefix}.1.Name", "Name"))
        params.append((f"{attr_prefix}.1.Value.StringValue", event["name"]))
        params.append((f"{attr_prefix}.1.Value.DataType", "String"))
        params.append((f"{attr_prefix}.2.Name", "Headers"))
        params.append((f"{attr_prefix}.2.Value.DataType", "String"))
        params.append((f"{attr_prefix}.2.Value.StringValue", json.dumps(event["headers"])))
    return urlencode(params)
```

#### Step 2: Rewrite `report_play`

Replace the current `report_play` body:

```python
def report_play(self, track_id: str, duration_seconds: int) -> bool:
    try:
        session = self._ensure_session()
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - int(duration_seconds * 1000)
        event_uuid = str(uuid.uuid4())
        playback_session_id = str(uuid.uuid4())

        payload = {
            "playbackSessionId": playback_session_id,
            "actualProductId": str(track_id),
            "requestedProductId": str(track_id),
            "productType": "TRACK",
            "actualAssetPresentation": "FULL",
            "actualAudioMode": "STEREO",
            "actualQuality": "LOSSLESS",  # see quality note below
            "sourceType": "PLAYLIST",
            "sourceId": "",
            "isPostPaywall": True,
            "startAssetPosition": 0.0,
            "endAssetPosition": float(duration_seconds),
            "startTimestamp": start_ms,
            "endTimestamp": now_ms,
            "actions": [
                {"actionType": "PLAYBACK_START", "assetPosition": 0.0, "timestamp": start_ms},
                {"actionType": "PLAYBACK_STOP", "assetPosition": float(duration_seconds), "timestamp": now_ms},
            ],
        }

        message_body = json.dumps({
            "name": "playback_session",
            "group": "play_log",
            "version": 2,
            "payload": payload,
            "ts": now_ms,
            "uuid": event_uuid,
        })

        headers_obj = {
            "app-name": "xmpd",
            "app-version": "0.1.0",
            "browser-name": "python-requests",
            "browser-version": requests.__version__,
            "os-name": "Linux",
            "client-id": str(session.config.client_id),
            "consent-category": "NECESSARY",
            "requested-sent-timestamp": now_ms,
            "authorization": session.access_token,
        }

        event_entry = {
            "id": event_uuid,
            "name": "playback_session",
            "message_body": message_body,
            "headers": headers_obj,
        }

        body = _build_event_batch_body([event_entry])
        resp = requests.post(
            "https://tidal.com/api/event-batch",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {session.access_token}",
            },
            timeout=15,
        )

        if resp.ok:
            logger.debug("Tidal: reported play for %s (%ds)", track_id, duration_seconds)
            return True
        else:
            logger.warning("Tidal report_play HTTP %s for %s: %s", resp.status_code, track_id, resp.text[:200])
            return False

    except Exception as e:
        logger.warning("Tidal report_play failed for %s: %s", track_id, e)
        return False
```

#### Step 3: Add required imports

Add to the top of `tidal.py`:
```python
import json
import uuid
from urllib.parse import urlencode
```

Note: `json`, `time`, and `requests` are already imported.

#### Step 4: Quality metadata

For now, hardcode `actualQuality: "LOSSLESS"` since xmpd requests `["FLAC", "FLAC_HIRES"]` manifests. The quality label from `_to_shared_track` (HiRes, HiFi, 320k, 96k) maps as:

| Track quality label | actualQuality value |
|---|---|
| HiRes | HI_RES_LOSSLESS |
| HiFi | LOSSLESS |
| 320k | HIGH |
| 96k | LOW |

A more accurate approach: store the quality tier during `resolve_stream` / `_fetch_manifest` and reference it in `report_play`. This is optional for the MVP. If you implement it, add a `_last_quality: dict[str, str]` keyed by track_id that `_fetch_manifest` populates and `report_play` reads. Clean up entries after report.

#### Step 5: Token refresh handling

If the POST returns 401, attempt `_try_refresh_session()` and retry once (same pattern as `resolve_stream`).

### What NOT to change

- Do NOT modify `xmpd/history_reporter.py` -- the `report_play` interface is sufficient
- Do NOT modify `xmpd/providers/base.py` -- the Provider protocol stays unchanged
- Do NOT add new config keys unless needed for quality overrides

---

## Dependencies

**Requires**: None

**Enables**: Nothing (all phases are independent)

---

## Completion Criteria

- [ ] `report_play` builds and POSTs a valid SQS-formatted event-batch payload
- [ ] Payload contains correct: playback_session event with track ID, timestamps, actions, quality
- [ ] HTTP response is checked; 401 triggers token refresh + retry
- [ ] Errors are caught and logged; method never raises (best-effort)
- [ ] Unit tests cover: SQS encoding, payload construction, timestamp calculation, error handling
- [ ] Existing test suite passes: `uv run pytest tests/ -v`
- [ ] Live verification: play a Tidal track for >30s, check daemon logs for successful report

---

## Testing Requirements

- Unit tests for `_build_event_batch_body`: verify correct SQS parameter encoding
- Unit tests for payload construction: verify JSON structure, timestamp math, UUID generation
- Unit tests for error handling: mock requests.post to return 401, 500, timeout
- Unit test for 401 retry: mock initial 401, then success after refresh
- Integration test (manual): play Tidal track, verify daemon log shows successful report

---

## External Interfaces Consumed

- **POST https://tidal.com/api/event-batch**
  - **Consumed by**: `xmpd/providers/tidal.py` (the new `report_play` implementation)
  - **How to capture**: After implementing, play a Tidal track for >30s and check daemon logs. Also useful: `curl -X POST https://tidal.com/api/event-batch -H "Authorization: Bearer $(python3 -c 'from xmpd.auth.tidal_oauth import load_session; from pathlib import Path; s=load_session(Path.home()/".config/xmpd/tidal_session.json"); print(s.access_token)')" -H "Content-Type: application/x-www-form-urlencoded" -d "SendMessageBatchRequestEntry.1.Id=test&SendMessageBatchRequestEntry.1.MessageBody={}" --verbose` to verify endpoint reachability and auth.
  - **If not observable**: Build the payload, log it, verify structure in tests. The actual POST can be verified after the service is restarted with the new code.

---

## Technical Reference

### Tidal Event-Batch API

**Endpoint**: `POST https://tidal.com/api/event-batch`

**Authentication**: `Authorization: Bearer <access_token>` HTTP header

**Content-Type**: `application/x-www-form-urlencoded` (NOT JSON)

**Payload format (SQS SendMessageBatchRequestEntry)**:

Each event N (1-indexed) encodes as:
```
SendMessageBatchRequestEntry.N.Id=<event_uuid>
SendMessageBatchRequestEntry.N.MessageBody=<JSON_stringified_event>
SendMessageBatchRequestEntry.N.MessageAttribute.1.Name=Name
SendMessageBatchRequestEntry.N.MessageAttribute.1.Value.StringValue=<event_name>
SendMessageBatchRequestEntry.N.MessageAttribute.1.Value.DataType=String
SendMessageBatchRequestEntry.N.MessageAttribute.2.Name=Headers
SendMessageBatchRequestEntry.N.MessageAttribute.2.Value.DataType=String
SendMessageBatchRequestEntry.N.MessageAttribute.2.Value.StringValue=<JSON_stringified_headers>
```

Max 10 events per batch.

**Event JSON (MessageBody)**:

```json
{
  "name": "playback_session",
  "group": "play_log",
  "version": 2,
  "payload": {
    "playbackSessionId": "<uuid4>",
    "actualProductId": "<track_id>",
    "requestedProductId": "<track_id>",
    "productType": "TRACK",
    "actualAssetPresentation": "FULL",
    "actualAudioMode": "STEREO",
    "actualQuality": "LOSSLESS",
    "sourceType": "PLAYLIST",
    "sourceId": "",
    "isPostPaywall": true,
    "startAssetPosition": 0.0,
    "endAssetPosition": 185.2,
    "startTimestamp": 1714400000000,
    "endTimestamp": 1714400185200,
    "actions": [
      {"actionType": "PLAYBACK_START", "assetPosition": 0.0, "timestamp": 1714400000000},
      {"actionType": "PLAYBACK_STOP", "assetPosition": 185.2, "timestamp": 1714400185200}
    ]
  },
  "ts": 1714400185200,
  "uuid": "<event_instance_uuid>"
}
```

**Headers object (MessageAttribute.2)**:

```json
{
  "app-name": "xmpd",
  "app-version": "0.1.0",
  "browser-name": "python-requests",
  "browser-version": "2.32",
  "os-name": "Linux",
  "client-id": "<session.config.client_id>",
  "consent-category": "NECESSARY",
  "requested-sent-timestamp": 1714400185200,
  "authorization": "<access_token_without_Bearer_prefix>"
}
```

Note: `authorization` in the headers object is the raw token (no "Bearer " prefix). The HTTP `Authorization` header uses "Bearer ".

**Quality mapping**:

| tidalapi quality | actualQuality value |
|---|---|
| HIRES_LOSSLESS tag | HI_RES_LOSSLESS |
| LOSSLESS tag | LOSSLESS |
| audio_quality HIGH | HIGH |
| audio_quality LOW | LOW |

**Response**: XML in SQS `SendMessageBatchResponse` format. Check `resp.ok` (status 200) for success. Detailed error parsing is optional.

**Key gotchas**:
- All timestamps are **unix epoch milliseconds**, not seconds
- `version: 2` at event level is required
- `group` must be `"play_log"` for authenticated users
- Content-Type is form-urlencoded despite MessageBody being JSON strings
- `client-id` is the tidalapi `session.config.client_id`, not `client_unique_key`

**Source**: Reverse-engineered from `tidal-music/tidal-sdk-web` (GitHub), specifically:
- `packages/event-producer/src/utils/sqsParamsConverter.ts` (SQS encoding)
- `packages/event-producer/src/submit/submit.ts` (HTTP submission)
- `packages/player/src/internal/event-tracking/play-log/playback-session.ts` (payload structure)

---

## Notes

- The `report_play` method is best-effort: it must never raise. Wrap everything in try/except.
- The current implementation's `track.get_stream()` call is slow (makes an HTTP request to fetch a stream URL). The new implementation is faster (just constructs a payload and POSTs).
- `uuid` is in Python stdlib, no new dependency needed.
- `urlencode` is from `urllib.parse`, also stdlib.
