"""
Streaming Constants - Shared constants for streaming responses.

All streaming endpoints should use these constants for consistent
anti-buffering behavior.

Extracted from gcli2api/src/streaming_constants.py for akarins-gateway.
"""

# [FIX 2026-02-17] Streaming response anti-buffering headers
# Ref: Antigravity-Manager v4.1.15 flush optimization
# Prevents nginx/cloudflare etc. from buffering SSE streams
#
# Fields:
# - X-Accel-Buffering: no  -> Tell nginx to disable proxy buffering
# - Cache-Control           -> Disable caching, ensure real-time delivery
# - Connection: keep-alive  -> Keep long-lived connection
STREAMING_HEADERS = {
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Connection": "keep-alive",
}
