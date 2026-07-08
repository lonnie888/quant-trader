"""SSE (Server-Sent Events) endpoint for real-time updates."""

import json
import logging
import time
from datetime import datetime, timezone

from flask import Blueprint, Response, stream_with_context

from .api import _read_ledger, _current_open_positions, _build_positions_data, _compute_summary

log = logging.getLogger(__name__)
sse_bp = Blueprint("sse", __name__)


@sse_bp.route("/events")
def events():
    def event_stream():
        while True:
            try:
                events_data = _read_ledger()
                open_evs = _current_open_positions(events_data)
                positions_data = _build_positions_data(open_evs)
                summary_data = _compute_summary(open_evs, events_data)

                yield f"event: positions\ndata: {json.dumps(positions_data, default=str)}\n\n"
                yield f"event: summary\ndata: {json.dumps(summary_data, default=str)}\n\n"
            except Exception as e:
                log.error("SSE error: %s", e)
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

            time.sleep(15)

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )