"""Modal deployment of the self-hosted transcription engine (CL-40 GPU variant).

Same FastAPI service as backend/selfhosted_service.py, running on a Modal
GPU container instead of Cloud Run/Lightning. Deploy with:

    modal deploy modal_app.py

Nothing under backend/ changes for this — transcribe_selfhosted.py already
reads WHISPER_DEVICE / WHISPER_COMPUTE_TYPE / PIPELINE_DEVICE from the
environment (see Dockerfile.selfhosted.gpu), so this file just sets those
and runs the same code on a GPU container.
"""

import modal

app = modal.App("callloop-transcribe-gpu")

hf_secret = modal.Secret.from_name("hf-token")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(
        "requirements-selfhosted.txt",
        extra_index_url="https://download.pytorch.org/whl/cu121",
    )
    # faster-whisper's CTranslate2 backend dlopen()s these directly at
    # inference time — it does not reuse torch's own bundled CUDA libs.
    .pip_install("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*")
    .add_local_python_source("backend", copy=True)
    # Bake weights in CPU mode (no GPU during an image build), same reason
    # as Dockerfile.selfhosted.gpu: this step only needs to download/cache
    # the models. WHISPER_DEVICE/PIPELINE_DEVICE default to "cpu" here since
    # .env(...) below hasn't been applied to the image yet at this point.
    .run_commands(
        'python -c "from backend.transcribe_selfhosted import get_pipeline, get_whisper; '
        'get_pipeline(); get_whisper()"',
        secrets=[hf_secret],
        env={"PYTHONPATH": "/root"},
    )
    .env(
        {
            "WHISPER_DEVICE": "cuda",
            "WHISPER_COMPUTE_TYPE": "float16",
            "PIPELINE_DEVICE": "cuda",
        }
    )
)


@app.function(
    image=image,
    gpu="T4",
    secrets=[hf_secret],
    timeout=1800,
    scaledown_window=300,
)
@modal.concurrent(max_inputs=1)
@modal.asgi_app()
def fastapi_app():
    import os

    # ctranslate2 looks these up via LD_LIBRARY_PATH at dlopen() time, which
    # reads the current process environment — setting this here, before the
    # lazy `from faster_whisper import WhisperModel` inside
    # transcribe_selfhosted.py runs, is enough (no shell/subprocess needed).
    import nvidia.cublas.lib
    import nvidia.cudnn.lib

    # __file__ is None here — these are namespace packages with no backing
    # __init__.py, just a directory of .so files — so __path__ (present on
    # every package, namespace or not) is what's actually reliable.
    os.environ["LD_LIBRARY_PATH"] = ":".join(
        p
        for p in [
            next(iter(nvidia.cublas.lib.__path__), ""),
            next(iter(nvidia.cudnn.lib.__path__), ""),
            os.environ.get("LD_LIBRARY_PATH", ""),
        ]
        if p
    )

    from backend.selfhosted_service import app as web_app

    return web_app
