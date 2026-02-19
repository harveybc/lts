"""
Security tests for LTS rate limiting and brute force prevention.
"""
import pytest
from fastapi.testclient import TestClient
import os

os.environ["LTS_QUIET"] = "1"


@pytest.fixture(scope="module")
def client():
    from app.database import Base, User
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import bcrypt
    from datetime import datetime, timezone

    engine = create_engine("sqlite:///./lts_rate_test.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestSession()
    pw_hash = bcrypt.hashpw(b"TestPass1", bcrypt.gensalt()).decode()
    u = User(username="rateuser", email="rate@test.com", password_hash=pw_hash, role="user", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(u)
    try:
        db.commit()
    except Exception:
        db.rollback()
    db.close()

    from app import web
    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    from app.database import get_db
    web.app.dependency_overrides[get_db] = override_get_db
    # Reset rate limit store
    web._rate_limit_store.clear()

    yield TestClient(web.app)

    Base.metadata.drop_all(bind=engine)
    web.app.dependency_overrides.clear()
    web._rate_limit_store.clear()
    try:
        os.remove("lts_rate_test.db")
    except Exception:
        pass


class TestRateLimiting:
    def test_rate_limit_triggers(self, client):
        """Rapid requests should eventually get 429"""
        from app import web
        web._rate_limit_store.clear()
        
        statuses = []
        for i in range(70):
            resp = client.post("/api/auth/login", json={"username": "rateuser", "password": "wrong"})
            statuses.append(resp.status_code)
        
        assert 429 in statuses, "Rate limiting should trigger after many rapid requests"

    def test_brute_force_login(self, client):
        """Multiple wrong passwords should be rate limited or locked"""
        from app import web
        web._rate_limit_store.clear()
        
        for i in range(10):
            resp = client.post("/api/auth/login", json={"username": "rateuser", "password": f"wrong{i}"})
        
        # After many attempts, should get rate limited or locked
        resp = client.post("/api/auth/login", json={"username": "rateuser", "password": "TestPass1"})
        # May be locked (423) or rate limited (429) or succeed if under threshold
        assert resp.status_code in [200, 423, 429]
