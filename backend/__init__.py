"""CallProof backend package. Load repo-root .env before sibling modules read os.getenv."""

from .config import load_env

load_env()
