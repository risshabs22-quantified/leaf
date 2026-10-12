import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import RateLimiter, app
from tests.conftest import DIAG_HIGH, OBS, PLAN, make_leaf


@pytest.fixture
def client(settings):
    leaf, _, _ = make_leaf(
        settings, {"vision": OBS, "diagnose": DIAG_HIGH, "plan": PLAN, "chat": "Sure [1]."}
    )
    app.state.leaf = leaf
    with TestClient(app) as c:
        app.state.limiter = RateLimiter(per_ip_per_hour=3, daily_cap=100)
        yield c
    app.state.leaf = None


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1500), (40, 120, 40)).save(buf, format="PNG")
    return buf.getvalue()


def test_health_reports_routing(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["models"]["reason"].startswith("nvidia/")


def test_diagnose_needs_photo_or_notes(client):
    assert client.post("/api/diagnose", data={}).status_code == 400


def test_diagnose_with_photo(client):
    r = client.post(
        "/api/diagnose",
        files={"image": ("leaf.png", png_bytes(), "image/png")},
        data={"region": "Texas"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diagnosis"]["candidates"][0]["name"] == "Early blight"
    assert {t["step"] for t in body["trace"]} == {"vision", "diagnose", "plan"}


def test_rejects_non_image(client):
    r = client.post("/api/diagnose", files={"image": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_rate_limit(client):
    codes = [client.post("/api/diagnose", data={"notes": "spots"}).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_chat_roundtrip(client):
    diag = client.post("/api/diagnose", data={"notes": "spots"}).json()
    r = client.post("/api/chat", json={"context": diag, "message": "Is it contagious?"})
    assert r.status_code == 200
    assert r.json()["reply"] == "Sure [1]."


def test_rejects_oversized_upload(client):
    big = b"\0" * (10 * 1024 * 1024 + 1)
    r = client.post("/api/diagnose", files={"image": ("big.png", big, "image/png")})
    assert r.status_code == 413


def test_client_ip_uses_last_forwarded_hop():
    from starlette.requests import Request

    from backend.app import client_ip

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")],
        "client": ("10.0.0.1", 1234),
    }
    assert client_ip(Request(scope)) == "203.0.113.9"
