"""CL-10: RLS policies in Alembic; API binds app.current_org_id from JWT org."""

from __future__ import annotations

from pathlib import Path

from backend.org_ids import DEFAULT_ORG_ID, bind_org_id, org_scope, reset_org_id

ROOT = Path(__file__).resolve().parent.parent
REV = ROOT / "alembic" / "versions" / "0005_rls.py"
TABLES = ("orgs", "calls", "segments", "audits", "rubrics", "api_usage")


def _sql() -> str:
    return REV.read_text(encoding="utf-8")


def test_rls_revision_enables_all_data_tables():
    raw = _sql()
    sql = raw.upper()
    assert REV.is_file()
    assert "ENABLE ROW LEVEL SECURITY" in sql
    for table in TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY".upper() in sql


def test_rls_revision_defines_crud_policies_per_table():
    sql = _sql().upper()
    for table in TABLES:
        for action in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert f"CREATE POLICY {table.upper()}_{action} ON {table.upper()}" in sql


def test_rls_policies_key_on_authenticated_org():
    raw = _sql()
    assert "callproof_current_org_id" in raw
    assert "app.current_org_id" in raw
    assert "org_id = public.callproof_current_org_id()" in raw
    assert "id = public.callproof_current_org_id()" in raw


def test_service_role_bypass_is_documented_and_limited():
    raw = _sql().lower()
    assert "service_role" in raw
    assert "alembic" in raw
    assert "backfill" in raw
    assert "callproof_app" in raw
    assert "nobypassrls" in raw
    assert "grant callproof_app to current_user" in raw
    assert "org_members is not rls" in raw or "org_members is not rls'd" in raw


def test_policies_are_not_applied_from_backend_python():
    backend = ROOT / "backend"
    hits: list[str] = []
    for path in backend.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            upper = line.upper()
            if "ENABLE ROW LEVEL SECURITY" in upper or "CREATE POLICY" in upper:
                hits.append(f"{path.name}:{i}")
    assert hits == [], "RLS belongs in alembic/versions/0005_rls.py: " + ", ".join(hits)


def test_api_handlers_do_not_bypass_rls():
    for name in (
        "api.py",
        "auth.py",
        "admin_provision.py",
        "transcribe.py",
        "qa_engine.py",
        "audit_store.py",
        "pyai_usage.py",
    ):
        text = (ROOT / "backend" / name).read_text(encoding="utf-8")
        assert "bypass_rls=True" not in text, name


def test_apply_tenant_gucs_is_parameterized():
    from backend.db import apply_tenant_gucs

    calls: list[tuple] = []

    class _Conn:
        def execute(self, sql, params=None):
            calls.append((str(sql), params))

    token = bind_org_id(DEFAULT_ORG_ID)
    try:
        apply_tenant_gucs(_Conn())
    finally:
        reset_org_id(token)

    assert calls[0][0] == "SELECT set_config(%s, %s, true)"
    assert ("role", "callproof_app") in [c[1] for c in calls]
    assert ("app.current_org_id", DEFAULT_ORG_ID) in [c[1] for c in calls]
    assert calls[2][1][0] == "app.current_user_id"
    assert DEFAULT_ORG_ID not in calls[0][0]


def test_apply_tenant_gucs_empty_when_unbound():
    from backend.db import apply_tenant_gucs

    calls: list[tuple] = []

    class _Conn:
        def execute(self, sql, params=None):
            calls.append((str(sql), params))

    apply_tenant_gucs(_Conn())
    params = [c[1] for c in calls]
    assert ("role", "callproof_app") in params
    assert ("app.current_org_id", "") in params
    assert ("app.current_user_id", "") in params


def test_org_scope_feeds_apply_tenant_gucs():
    from backend.db import apply_tenant_gucs

    other = "00000000-0000-4000-8000-000000000099"
    calls: list[tuple] = []

    class _Conn:
        def execute(self, sql, params=None):
            calls.append(params)

    with org_scope(other):
        apply_tenant_gucs(_Conn())
    assert ("app.current_org_id", other) in calls


def test_live_relrowsecurity_if_migrated():
    """If 0005 is applied, pg_class.relrowsecurity is on for tenant tables."""
    import pytest
    from dotenv import load_dotenv
    from psycopg.rows import dict_row

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg

    conn = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    try:
        fn = conn.execute(
            """
            SELECT 1 FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = 'callproof_current_org_id'
            """
        ).fetchone()
        if not fn:
            pytest.skip("0005_rls not applied")
        rows = conn.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY(%s)
              AND c.relrowsecurity
            """,
            (list(TABLES),),
        ).fetchall()
        found = {r["relname"] for r in rows}
        assert found == set(TABLES)
        members = conn.execute(
            """
            SELECT c.relrowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = 'org_members'
            """
        ).fetchone()
        if members:
            assert members["relrowsecurity"] is False
        policies = conn.execute(
            """
            SELECT tablename, policyname
            FROM pg_policies
            WHERE schemaname = 'public' AND tablename = ANY(%s)
            """,
            (list(TABLES),),
        ).fetchall()
        names = {(r["tablename"], r["policyname"]) for r in policies}
        for table in TABLES:
            for action in ("select", "insert", "update", "delete"):
                assert (table, f"{table}_{action}") in names
    finally:
        conn.close()


def test_api_sets_non_bypass_role():
    text = (ROOT / "backend" / "db.py").read_text(encoding="utf-8")
    assert 'APP_ROLE = "callproof_app"' in text
    assert '("role", APP_ROLE)' in text
    assert "bypass_rls=True" not in (ROOT / "backend" / "api.py").read_text(encoding="utf-8")


def test_live_api_connection_does_not_bypass_rls():
    """CL-10 AC: db.connection() is callproof_app (rolbypassrls=false) and hides other orgs."""
    import uuid

    import pytest
    from dotenv import load_dotenv
    from psycopg.rows import dict_row

    from backend.db import APP_ROLE, connection
    from backend.db_url import database_url, psycopg_url
    from backend.org_ids import DEFAULT_ORG_ID, org_scope
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    org_b = str(uuid.uuid4())
    identity = f"test://cl10/{uuid.uuid4().hex}"
    try:
        role = admin.execute(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = %s",
            (APP_ROLE,),
        ).fetchone()
        if not role:
            pytest.skip("0005_rls not applied")
        assert role["rolbypassrls"] is False

        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_b, "cl10-b"))
        admin.execute(
            """
            INSERT INTO calls (org_id, audio_url, status)
            VALUES (%s, %s, 'completed')
            """,
            (org_b, identity),
        )
        admin.commit()

        with org_scope(DEFAULT_ORG_ID):
            with connection() as conn:
                who = conn.execute(
                    """
                    SELECT current_user AS u, r.rolbypassrls
                    FROM pg_roles r
                    WHERE r.rolname = current_user
                    """
                ).fetchone()
                assert who["u"] == APP_ROLE
                assert who["rolbypassrls"] is False
                leaked = conn.execute(
                    "SELECT id FROM calls WHERE audio_url = %s",
                    (identity,),
                ).fetchone()
                assert leaked is None

        with org_scope(org_b):
            with connection() as conn:
                seen = conn.execute(
                    "SELECT id FROM calls WHERE audio_url = %s",
                    (identity,),
                ).fetchone()
                assert seen is not None
    finally:
        admin.rollback()
        admin.execute("DELETE FROM calls WHERE audio_url = %s", (identity,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_b,))
        admin.commit()
        admin.close()
