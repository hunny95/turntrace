"""TurnTrace backend HTTP server (Gate A).

Exposes the SmallWebRTC offer/answer endpoint used by the Next.js client and
starts one bot pipeline per connection. Local development only.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from bot import run_bot
from config import FRONTEND_ORIGIN, check_required_env_vars

# Fail fast with a names-only error if required provider credentials are missing.
check_required_env_vars()

small_webrtc_handler = SmallWebRTCRequestHandler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await small_webrtc_handler.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    """Handle a SmallWebRTC offer and start a bot pipeline for the connection."""

    async def webrtc_connection_callback(connection):
        background_tasks.add_task(run_bot, connection)

    answer = await small_webrtc_handler.handle_web_request(
        request=request,
        webrtc_connection_callback=webrtc_connection_callback,
    )
    return answer


@app.patch("/api/offer")
async def ice_update(request: SmallWebRTCPatchRequest):
    await small_webrtc_handler.handle_patch_request(request)
    return {"status": "success"}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TurnTrace backend")
    parser.add_argument("--host", default="localhost", help="Host for HTTP server")
    parser.add_argument("--port", type=int, default=7860, help="Port for HTTP server")
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    logger.remove(0)
    # DEBUG so the transcription/LLM log observers (which log at DEBUG) are visible.
    logger.add(sys.stderr, level="TRACE" if args.verbose else "DEBUG")

    uvicorn.run(app, host=args.host, port=args.port)
