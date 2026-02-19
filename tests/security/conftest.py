"""Conftest for LTS security tests."""
import pytest
import os
import sys

os.environ["LTS_QUIET"] = "1"

# Add project root to path
_LTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, _LTS_ROOT)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base, User, Session as UserSession, AuditLog, Portfolio, BillingRecord

TEST_DB_URL = "sqlite:///./lts_security_test.db"

@pytest.fixture(scope="module")
def test_engine():
    engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)

@pytest.fixture(scope="module")
def TestSession(test_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

@pytest.fixture(autouse=True)
def clean_db(test_engine):
    """Clean all tables before each test."""
    Base.metadata.create_all(bind=test_engine)
    yield
    # Don't drop - just clean
    with test_engine.connect() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.commit()
