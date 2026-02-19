"""
Security tests for LTS input validation: SQL injection, XSS, oversized requests, malformed JSON.
Tests the web.py Pydantic models and middleware.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import json
import os

os.environ["LTS_QUIET"] = "1"


@pytest.fixture(scope="module")
def client():
    """Create test client with mocked DB"""
    from app.database import Base, User
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import bcrypt

    engine = create_engine("sqlite:///./lts_input_test.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Create test user
    db = TestSession()
    pw_hash = bcrypt.hashpw(b"TestPass1", bcrypt.gensalt()).decode()
    from datetime import datetime, timezone
    u = User(username="testuser", email="test@test.com", password_hash=pw_hash, role="user", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(u)
    admin = User(username="admin", email="admin@test.com", password_hash=pw_hash, role="admin", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(admin)
    try:
        db.commit()
    except Exception:
        db.rollback()
    db.close()

    # Override get_db
    from app import web
    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    from app.database import get_db
    web.app.dependency_overrides[get_db] = override_get_db

    yield TestClient(web.app)

    # Cleanup
    Base.metadata.drop_all(bind=engine)
    web.app.dependency_overrides.clear()
    try:
        os.remove("lts_input_test.db")
    except Exception:
        pass


def get_token(client, username="testuser", password="TestPass1"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    if resp.status_code == 200:
        return resp.json()["access_token"]
    return None


class TestSQLInjection:
    def test_login_sql_injection_username(self, client):
        payloads = [
            "'; DROP TABLE users; --",
            "' OR '1'='1",
            "' UNION SELECT * FROM users --",
        ]
        for payload in payloads:
            resp = client.post("/api/auth/login", json={"username": payload, "password": "anything"})
            assert resp.status_code in [401, 422]

    def test_login_sql_injection_password(self, client):
        resp = client.post("/api/auth/login", json={"username": "testuser", "password": "' OR '1'='1"})
        assert resp.status_code == 401


class TestXSSPrevention:
    def test_xss_in_registration(self, client):
        payloads = [
            "<script>alert('xss')</script>",
            "javascript:alert(1)",
            '<img src=x onerror=alert(1)>',
        ]
        for payload in payloads:
            resp = client.post("/api/auth/register", json={
                "username": payload, "email": "xss@test.com", "password": "XssTest1"
            })
            # Should reject due to pattern validation
            assert resp.status_code in [400, 422]


class TestOversizedRequests:
    def test_large_payload_login(self, client):
        resp = client.post("/api/auth/login", json={
            "username": "A" * 100000,
            "password": "B" * 100000
        })
        assert resp.status_code in [413, 422]

    def test_large_username_register(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "A" * 1000,
            "email": "big@test.com",
            "password": "BigPass1"
        })
        assert resp.status_code == 422  # max_length validation


class TestMalformedInput:
    def test_empty_json(self, client):
        resp = client.post("/api/auth/login", json={})
        assert resp.status_code == 422

    def test_invalid_json(self, client):
        resp = client.post("/api/auth/login", content=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 422

    def test_missing_fields(self, client):
        resp = client.post("/api/auth/login", json={"username": "test"})
        assert resp.status_code == 422

    def test_null_values(self, client):
        resp = client.post("/api/auth/login", json={"username": None, "password": None})
        assert resp.status_code == 422


class TestPathTraversal:
    def test_path_traversal_in_username(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "../../../etc/passwd",
            "email": "path@test.com",
            "password": "PathTest1"
        })
        assert resp.status_code in [400, 422]


class TestSecurityHeaders:
    def test_security_headers_present(self, client):
        resp = client.get("/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert "Strict-Transport-Security" in resp.headers
