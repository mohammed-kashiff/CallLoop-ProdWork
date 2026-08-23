"""CLI shim. Prefer: python -m backend.qa_engine [call_id]"""

from backend.qa_engine import main

if __name__ == "__main__":
    main()
