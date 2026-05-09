"""Server-sent event stream for realtime dashboard updates."""
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from manager.app.events import wait_for_dashboard_update

router = APIRouter()


@router.get("/stream/dashboard")
def stream_dashboard_updates(
    last_seq: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Push lightweight dashboard update events to the frontend via SSE."""

    start_seq = last_seq
    if last_event_id is not None:
        try:
            start_seq = max(start_seq, int(last_event_id))
        except ValueError:
            pass

    def event_stream():
        current_seq = start_seq
        while True:
            seq, event_name, payload = wait_for_dashboard_update(current_seq, timeout_seconds=15.0)
            if seq <= current_seq:
                # Keep-alive comment to prevent idle proxies from closing the stream.
                yield ": keepalive\n\n"
                continue
            current_seq = seq
            data = json.dumps(payload, separators=(",", ":"))
            yield f"id: {seq}\nevent: {event_name}\ndata: {data}\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
