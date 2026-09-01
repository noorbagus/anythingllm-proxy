"""csmart_proxy.py — shim (W3).

Forwarding entrypoint: the FastAPI app lives in csmart.app.factory. Keeps the
`python csmart_proxy.py` entrypoint working for existing launchers.
"""
from __future__ import annotations

import uvicorn

from any_proxy.app.config import PROXY_HOST, PROXY_PORT
from any_proxy.app.factory import app
from any_proxy.logging.structured import _banner


if __name__ == "__main__":
    _banner()
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="warning")