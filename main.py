"""DeepSeek Browser Bridge — 本地入口。

启动: python main.py   ->  http://127.0.0.1:8765
"""
import logging

import uvicorn

from bridge.config import HOST, PORT
from bridge.server import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
