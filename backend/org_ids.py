"""Well-known org id for the single-tenant placeholder row (CL-4).

Auth (CL-8) will mint real orgs. Until then every migrated row uses DEFAULT_ORG_ID.
Never accept org_id from client input — later tickets derive it from the JWT.
"""

from __future__ import annotations

# UUID v4-shaped, stable across environments. Seeded in Alembic revision 0001.
DEFAULT_ORG_ID = "00000000-0000-4000-8000-000000000001"
