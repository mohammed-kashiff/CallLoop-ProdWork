"""Anthropic connectivity smoke. Usage: python test_claude.py"""

import os
import sys

import httpx
from dotenv import load_dotenv

from backend.paths import ENV_FILE

load_dotenv(ENV_FILE)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    sys.exit("ERROR: ANTHROPIC_API_KEY not found. Set it on the host environment.")

MODEL = "claude-haiku-4-5"

resp = httpx.post(
    "https://api.anthropic.com/v1/messages",
    headers={
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    },
    json={
        "model": MODEL,
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": "Reply with exactly this phrase: CallProof LLM link OK"}
        ],
    },
    timeout=30,
)

if resp.status_code != 200:
    sys.exit(f"ERROR {resp.status_code}: {resp.text}")

data = resp.json()
text = "".join(
    block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
)
usage = data.get("usage", {})

print("Claude replied:", text.strip())
print(f"Model: {data.get('model')}")
print(f"Tokens -> input: {usage.get('input_tokens')}, output: {usage.get('output_tokens')}")
print("\nLLM link verified. Ready to build the QA engine.")
