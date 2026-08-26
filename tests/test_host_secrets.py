"""App-owned secrets stay on the host. .env is gitignored and not in the tree."""

from __future__ import annotations

import subprocess

from backend.paths import ROOT


def test_dotenv_is_gitignored_and_untracked():
    ig = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ig
    tracked = subprocess.check_output(
        ["git", "ls-files", "--", ".env", "frontend/.env"],
        cwd=ROOT,
        text=True,
    ).strip()
    assert tracked == ""
    ignored = subprocess.check_output(
        ["git", "check-ignore", "-v", ".env", "frontend/.env"],
        cwd=ROOT,
        text=True,
    )
    assert ".env" in ignored
    example = subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"],
        cwd=ROOT,
    )
    assert example.returncode == 1


def test_runtime_does_not_write_app_secrets_to_dotenv():
    api = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    store = (ROOT / "backend" / "env_keys.py").read_text(encoding="utf-8")
    assert "upsert_env_value" not in api
    assert "upsert_env_value" not in store
    assert "_write_key_to_env" not in api
    assert "_mint_sandbox_key" not in api


def test_readme_lists_required_env_names_without_values():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in (
        "PYAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
    ):
        assert name in readme
    assert "PYAI_API_KEY=pyai_live_your_key_here" not in readme
    assert "ANTHROPIC_API_KEY=sk-ant-" not in readme
