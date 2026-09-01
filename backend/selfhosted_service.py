"""Thin HTTP wrapper around the self-hosted engine (CL-40). Runs on Cloud
Run, never on the box serving live API traffic — that's the whole point.

No transcription logic lives here. This just exposes transcribe_selfhosted()
(CL-38, already tested on its own) over HTTP so Render can call it remotely
instead of running torch/pyannote in-process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile

from . import applog
from .transcribe_selfhosted import transcribe_selfhosted

# applog.setup_logging() (called by transcribe_selfhosted on import) only adds
# a file handler — console output has always depended on some other module
# calling basicConfig() first (transcribe.py does this for the main app).
# This service has no such module, so without this call every log line here
# was going nowhere Cloud Run could ever show it.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("callproof.selfhosted_service")

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    suffix = os.path.splitext(audio.filename or "")[1] or ".audio"
    fd, path = tempfile.mkstemp(prefix="selfhosted_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(await audio.read())
        job_id, result, mode = await asyncio.to_thread(transcribe_selfhosted, path)
        return {"job_id": job_id, "result": result, "mode": mode}
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "selfhosted_service_failed", level=logging.ERROR, error=str(e)[:300],
        )
        # This service requires Google IAM auth (Cloud Run rejects anything
        # unauthenticated before it reaches this code) — only Render's own
        # service account ever sees this response, so the real exception is
        # safe to return directly instead of a generic message.
        raise HTTPException(
            status_code=502, detail=f"Self-hosted transcription failed: {e}"[:400],
        ) from e
    finally:
        if os.path.exists(path):
            os.remove(path)
