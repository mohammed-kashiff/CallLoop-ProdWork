"""CLI shim. Prefer: python -m backend.transcribe /path/to.mp3"""

from backend.transcribe import main

if __name__ == "__main__":
    main()
