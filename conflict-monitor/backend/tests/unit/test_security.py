"""Who may call what: C60 (CORS, websocket) and C62 (admin routes).

Found as: `allow_origins=["*"]` with credentials, which Starlette answers by
reflecting ANY Origin on preflight, so any web page the operator had open could
DELETE the archive; and seven /events/admin/* routes with no auth at all.

These run the real app in-process (Starlette's TestClient, no lifespan, so no
pollers start and nothing touches the network or the database).
"""

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import security
from app.config import settings
from app.main import app

TOKEN = "s3cret-admin-token"
FRONTEND = "http://localhost:5173"
ATTACKER = "https://attacker.example"


# Captured at import, before conftest's _ban_network patches httpx.Client.send.
# TestClient is an httpx.Client whose transport calls the ASGI app in-process,
# so re-allowing send on TestClient ONLY leaves every real client banned.
_IN_PROCESS_SEND = httpx.Client.send


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(TestClient, "send", _IN_PROCESS_SEND)
    return TestClient(app)  # no `with`: the lifespan (pollers, DB) never runs


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    return TOKEN


# ── Admin routes (C62) ───────────────────────────────────────────────────────

ADMIN = [
    ("post", "/events/admin/fix-null-coords"),
    ("post", "/events/admin/reclassify-locations"),
    ("post", "/events/admin/backfill"),
    ("delete", "/events/admin/purge-old?before=2030-01-01"),
    ("delete", "/events/admin/dedup"),
    ("get", "/events/admin/geo-stats"),
    ("post", "/events/admin/import-osint-dataset"),
]


@pytest.mark.parametrize("method, path", ADMIN)
def test_no_admin_token_configured_closes_the_admin_routes(client, monkeypatch, method, path):
    """Unset closes the door; it never opens it."""
    monkeypatch.setattr(settings, "admin_token", "")
    r = getattr(client, method)(path, headers={"X-Admin-Token": ""})
    assert r.status_code == 503
    assert "ADMIN_TOKEN" in r.json()["detail"]


@pytest.mark.parametrize("method, path", ADMIN)
def test_a_missing_or_wrong_token_is_refused(client, token, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-Admin-Token": "guess"}).status_code == 401


def test_the_right_token_reaches_the_handler(client, token, monkeypatch):
    """Through the gate, not just refused by it. The handler is swapped for a
    stub so no background sweep starts."""
    calls = []
    monkeypatch.setattr("app.routes.events._fix_null_coords_task", lambda: calls.append(1))
    r = client.post("/events/admin/fix-null-coords", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200 and r.json()["status"] == "started"
    assert calls == [1]


def test_every_admin_path_and_every_write_is_behind_the_token():
    """A route added later with a bare @router.delete fails here, not in
    production. GETs outside /admin are the public read API."""
    unguarded = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        guarded = any(d.call is security.require_admin_token for d in route.dependant.dependencies)
        writes = route.methods - {"GET", "HEAD"}
        if ("/admin" in route.path or writes) and not guarded:
            unguarded.append((sorted(route.methods), route.path))
    assert unguarded == []


def test_public_reads_need_no_token(client):
    assert client.get("/config").status_code == 200


# ── CORS (C60) ───────────────────────────────────────────────────────────────

def _preflight(client, origin, method):
    return client.options("/events/admin/purge-old", headers={
        "Origin": origin, "Access-Control-Request-Method": method,
    })


def test_a_foreign_origin_is_not_reflected_on_preflight(client):
    """The exact request from the C60 finding: it used to answer 200 with the
    attacker's origin and credentials allowed."""
    r = _preflight(client, ATTACKER, "DELETE")
    assert r.status_code == 400
    assert r.headers.get("access-control-allow-origin") != ATTACKER
    assert "access-control-allow-credentials" not in r.headers


def test_the_frontend_may_read_but_not_write_and_sends_no_credentials(client):
    ok = _preflight(client, FRONTEND, "GET")
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == FRONTEND
    assert "access-control-allow-credentials" not in ok.headers
    assert _preflight(client, FRONTEND, "DELETE").status_code == 400


def test_a_simple_get_from_a_foreign_origin_gets_no_allow_origin(client):
    r = client.get("/config", headers={"Origin": ATTACKER})
    assert "access-control-allow-origin" not in r.headers  # the browser withholds the body


# ── Websocket (C60) ──────────────────────────────────────────────────────────

def test_the_websocket_refuses_a_foreign_origin(client):
    # Refused at the handshake: entering the context raises. (Waiting on a
    # receive instead would hang until the server's 60 s ping if it accepted.)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws/events", headers={"Origin": ATTACKER}):
            pass
    assert e.value.code == 1008


def test_the_websocket_accepts_the_frontend(client, monkeypatch):
    connected = []

    async def connect(ws):
        await ws.accept()
        connected.append(ws)

    async def disconnect(ws):
        pass

    monkeypatch.setattr("app.routes.ws.broadcaster.connect", connect)
    monkeypatch.setattr("app.routes.ws.broadcaster.disconnect", disconnect)
    with client.websocket_connect("/ws/events", headers={"Origin": FRONTEND}):
        pass
    assert len(connected) == 1


def test_origin_list_parsing_tolerates_spaces_and_trailing_slashes(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", " https://cm.example/ , http://localhost:5173")
    assert security.allowed_origins() == ["https://cm.example", "http://localhost:5173"]
    assert security.origin_allowed("https://cm.example")
    assert not security.origin_allowed("https://cm.example.attacker.net")
