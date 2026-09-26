"""Who may call what (C60, C62 in docs/FINDINGS.md).

- The admin routes (`/events/admin/*`) sit on their own router behind
  `require_admin_token`. With no ADMIN_TOKEN set they answer 503 rather than
  running: an unset secret closes the door, it never opens it.
- Browsers may read the API only from the origins in CORS_ORIGINS, with GET
  and no credentials. The frontend sends no cookie and makes no other call.
- The live websocket refuses a browser Origin outside the same list. CORS does
  not apply to websockets, so without this any page the operator has open
  could stream the feed. A client that sends no Origin is not a browser page;
  what keeps it out is the port binding (docker-compose.yml: loopback only).
"""

import hmac

from fastapi import Header, HTTPException

from app.config import settings

ADMIN_HEADER = "X-Admin-Token"


def allowed_origins() -> list[str]:
    return [o.strip().rstrip("/") for o in settings.cors_origins.split(",") if o.strip()]


def origin_allowed(origin: str | None) -> bool:
    if origin is None:
        return True
    return origin.rstrip("/") in allowed_origins()


async def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    expected = settings.admin_token
    if not expected:
        raise HTTPException(503, "admin routes are disabled: set ADMIN_TOKEN")
    # Constant-time: the comparison must not leak how much of a guess was right.
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(401, f"missing or wrong {ADMIN_HEADER} header")
