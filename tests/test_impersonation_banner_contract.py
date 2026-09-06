"""AC-34 Members-tab "Log in as" must use the same customer-origin handoff
the existing ImpersonationBanner already consumes (AC-18). Verification
lock, not a new banner: if the Members button drifts to a different query
shape or opens Command Center instead of call-loop.com, the real banner
would not show."""

from __future__ import annotations

from backend.paths import ROOT

FRONTEND = ROOT / "frontend" / "src"


def test_members_tab_log_in_as_uses_the_same_handoff_the_banner_reads():
    admin = (FRONTEND / "pages" / "Admin.tsx").read_text(encoding="utf-8")
    start = admin.index("const logInAsMember")
    end = admin.index("const sendResetEmailForMember", start)
    handoff = admin[start:end]
    assert "/api/admin/users/" in handoff
    assert "/impersonate" in handoff
    assert "impersonated: '1'" in handoff
    assert "org:" in handoff
    assert "as:" in handoff
    assert "body.target_email" in handoff
    assert "CUSTOMER_ORIGIN" in handoff
    assert "window.open" in handoff

    members = admin[admin.index("activeTab === 'members'"):]
    assert "logInAsMember(m)" in members
    assert "Log in as" in members

    supabase = (FRONTEND / "lib" / "supabase.ts").read_text(encoding="utf-8")
    assert "function adoptImpersonationUrl" in supabase
    assert "query.get('impersonated') !== '1'" in supabase
    assert "query.get('org')" in supabase
    assert "query.get('as')" in supabase
    assert "markImpersonating" in supabase

    banner = (FRONTEND / "components" / "ImpersonationBanner.tsx").read_text(
        encoding="utf-8",
    )
    assert "getImpersonating" in banner
    assert "info.orgName" in banner
    assert "info.targetEmail" in banner
    assert "clearImpersonating" in banner
    assert "signOut" in banner

    layout = (FRONTEND / "components" / "AppLayout.tsx").read_text(encoding="utf-8")
    assert "ImpersonationBanner" in layout
    assert "adminHost ? null : <ImpersonationBanner />" in layout

    host = (FRONTEND / "lib" / "adminHost.ts").read_text(encoding="utf-8")
    assert "CUSTOMER_ORIGIN = 'https://call-loop.com'" in host


def test_sign_out_clears_the_impersonation_hint():
    """Exit on the banner and Account-menu sign-out must both drop the
    sessionStorage flag. Otherwise a later login in the same customer tab
    would keep showing 'Viewing as support' for the previous member."""
    auth = (FRONTEND / "context" / "AuthContext.tsx").read_text(encoding="utf-8")
    start = auth.index("const signOut")
    end = auth.index("}, [])", start)
    assert "clearImpersonating" in auth[start:end]
