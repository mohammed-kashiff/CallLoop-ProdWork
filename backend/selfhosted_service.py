"""Thin HTTP wrapper around the self-hosted engine (CL-40). Runs on Cloud
Run, never on the box serving live API traffic — that's the whole point.

No transcription logic lives here. This just exposes transcribe_selfhosted()
(CL-38, already tested on its own) over HTTP so Render can call it remotely
instead of running torch/pyannote in-process.
"""

from __future__ import annotations

import logging
import os
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile

from . import applog
from .transcribe_selfhosted import transcribe_selfhosted

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
        job_id, result, mode = transcribe_selfhosted(path)
        return {"job_id": job_id, "result": result, "mode": mode}
    except Exception as e:  # noqa: BLE001
        applog.event(log, "selfhosted_service_failed", error=str(e)[:300])
        raise HTTPException(
            status_code=502, detail="Self-hosted transcription failed."
        ) from e
    finally:
        if os.path.exists(path):
            os.remove(path)
