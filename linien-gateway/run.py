from __future__ import annotations

from pathlib import Path

import uvicorn

from app.config import get_api_host, get_api_port


if __name__ == "__main__":
    app_dir = Path(__file__).resolve().parent
    uvicorn.run(
        "app.main:app",
        host=get_api_host(),
        port=get_api_port(),
        reload=True,
        app_dir=str(app_dir),
        # Enable WebSocket permessage-deflate. Compresses each frame
        # before transit. On the binary plot-frame path the win is
        # modest (~1.1-1.3x for high-entropy float32 traces); on the
        # JSON fallback path it's ~5x. Either way the gateway pays a
        # small extra CPU cost (<5 us/frame) which is well below the
        # build_plot_frame work it sits behind.
        ws_per_message_deflate=True,
    )
