"""Per-user identity for the multi-user API (Phase 4, §9, §11).

Auth is intentionally thin and pluggable. The plan names Supabase Auth / Clerk
as the production choice; both ultimately hand the backend a verified user id
(via a bearer JWT). This module isolates that single fact — "who is this
request?" — behind one dependency so the production swap is contained.

Dev/local scheme (no external IdP): a bearer token *is* the user id
(`Authorization: Bearer alice`). Verified user data — balances, card, prefs —
is loaded from the `Repository`, never from the request body, so a user can
only ever see verdicts computed against *their own* holdings.

Auth is **off by default** (`MILEAGE_AUTH` unset) so single-user/local runs and
the Phase 3 contract keep working with the request body as the source of truth.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException

from ..config import Config
from ..domain.models import User


def _extract_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip() or None


def resolve_user(
    repo,
    *,
    token: Optional[str],
    auth_enabled: bool,
) -> User:
    """Resolve the acting user from a bearer token.

    With auth enabled the token is the user id and the user must exist in the
    Repository (balances/card are server-side truth). With auth disabled we fall
    back to the shared single-user "local" account.
    """
    if not auth_enabled:
        user_id = token or "local"
        return repo.get_user(user_id) or User(user_id=user_id)

    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    user = repo.get_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return user


def make_current_user_dependency(get_config, get_orchestrator):
    """Build the `current_user` FastAPI dependency bound to app providers."""

    def current_user(
        authorization: Optional[str] = Header(default=None),
        config: Config = Depends(get_config),
        orchestrator=Depends(get_orchestrator),
    ) -> User:
        token = _extract_token(authorization)
        return resolve_user(
            orchestrator.repo,
            token=token,
            auth_enabled=config.auth_enabled,
        )

    return current_user
