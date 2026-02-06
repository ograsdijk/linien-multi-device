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
    )
