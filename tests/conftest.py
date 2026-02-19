# -*- coding: utf-8 -*-
"""
LTS (Live Trading System) - Test Configuration

This module provides common test fixtures and configuration for the LTS test suite.
It includes database setup, mock objects, and shared test utilities.
"""

import pytest
import os
import sys
import tempfile
import asyncio
from unittest.mock import Mock, MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.config_handler import ConfigHandler
from app.database import Database, db_session, Base, SyncSessionLocal, get_db
from fastapi.testclient import TestClient
from plugins_core.default_core import CorePlugin
from fastapi import FastAPI, Depends
import json

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

@pytest.fixture(scope="function")
def app(fresh_db, mock_config):
    """Create a FastAPI app for testing, with a fresh database."""
    import sys as _sys
    print(f"\n[DEBUG] fresh_db type: {type(fresh_db)}, db_url: {getattr(fresh_db, 'db_url', 'N/A')}", file=_sys.stderr)
    
    core_plugin = CorePlugin()
    
    # This is a simplified version of the dependency override
    # In a real app, you'd likely have a more robust way to manage this
    async def get_test_db():
        async with fresh_db.get_session() as session:
            yield session

    def get_sync_test_db():
        """Provides a synchronous session for plugins that need it."""
        # This uses the same underlying database file as the async session
        # but with a separate synchronous engine.
        sync_engine = create_engine(
            fresh_db.db_url.replace('+aiosqlite', ''), 
            connect_args={"check_same_thread": False}
        )
        TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    
    # Initialize the core plugin, which sets up its routes
    core_plugin.initialize(
        plugins={'core': core_plugin}, 
        config=mock_config, # Use the mock_config fixture
        database=fresh_db,
        get_db=get_sync_test_db # Pass the sync session provider
    )
    
    # Ensure all tables are created on the sync engine as well
    sync_engine = create_engine(
        fresh_db.db_url.replace('+aiosqlite', ''), 
        connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=sync_engine)


    # Override the dependencies in the core plugin's router
    app.dependency_overrides[get_db] = get_sync_test_db
    
    app.include_router(core_plugin.router)
    
    # Add a reference to the database object to the app for tests that need it
    app.database = fresh_db
    
    return app

@pytest.fixture(scope="function")
def client(app):
    """Create a TestClient for the app."""
    with TestClient(app) as c:
        yield c

# Note: event_loop fixture is provided by pytest-asyncio.
# Do not override it — the deprecated redefinition was removed.

@pytest.fixture(scope="function")
def fresh_db():
    """Create a fresh database for each test function."""
    import asyncio
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as temp_file:
        db_path = temp_file.name
    
    db = Database(db_path=db_path)
    
    # Initialize synchronously using the sync engine
    from sqlalchemy import create_engine as _ce
    _sync_url = db.db_url.replace('+aiosqlite', '')
    _engine = _ce(_sync_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=_engine)
    _engine.dispose()
    
    yield db
    
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass


@pytest.fixture(scope="function")
def app_with_config(fresh_db):
    """Create a FastAPI app with a specific test configuration."""
    with patch.dict(os.environ, {"LTS_DATABASE_PATH": "test_db_from_env.sqlite"}):
        # Create temporary config files
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f_default:
            json.dump({"DEFAULT_SETTING": "file_value", "LOG_LEVEL": "INFO"}, f_default)
            default_config_path = f_default.name

        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f_override:
            json.dump({"LOG_LEVEL": "DEBUG"}, f_override)
            override_config_path = f_override.name

        config_handler = ConfigHandler(default_file_path=default_config_path, override_file_path=override_config_path)
        config = config_handler.get_config()

        core_plugin = CorePlugin()
        app = FastAPI()

        def get_sync_test_db():
            sync_engine = create_engine(
                fresh_db.db_url.replace('+aiosqlite', ''),
                connect_args={"check_same_thread": False}
            )
            TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)
            db = TestSessionLocal()
            try:
                yield db
            finally:
                db.close()

        core_plugin.initialize(
            plugins={'core': core_plugin},
            config=config,
            database=fresh_db,
            get_db=get_sync_test_db
        )

        app.include_router(core_plugin.router)
        app.config = config  # Attach config for easy access in tests

        yield app

    # Cleanup config files
    os.unlink(default_config_path)
    os.unlink(override_config_path)

@pytest.fixture(scope="session")
def mock_config():
    """Create a mock configuration for testing."""
    return {
        'database': {
            'type': 'sqlite',
            'path': ':memory:'
        },
        'api': {
            'host': '127.0.0.1',
            'port': 8080,
            'debug': True
        },
        'security': {
            'secret_key': 'test_secret_key',
            'session_timeout': 3600
        },
        'logging': {
            'level': 'INFO',
            'file': 'test.log'
        }
    }

@pytest.fixture(scope="session")
def mock_plugin_loader():
    """Create a mock plugin loader for testing."""
    loader = Mock()
    loader.load_plugins = Mock()
    loader.shutdown_plugins = Mock()
    return loader

@pytest.fixture
def mock_database():
    """Create a mock database for testing."""
    db = Mock()
    db.initialize = Mock(return_value=True)
    db.cleanup = Mock()
    db.execute_sql = Mock()
    db.get_connection = Mock()
    return db

@pytest.fixture
def mock_plugins():
    """Create a set of mock plugins for testing."""
    plugins = {}
    
    # Mock AAA plugin
    mock_aaa = Mock()
    mock_aaa.initialize = Mock(return_value=True)
    mock_aaa.authenticate = Mock(return_value={'status': 'success', 'user_id': 'test_user'})
    mock_aaa.authorize = Mock(return_value=True)
    mock_aaa.log_activity = Mock()
    mock_aaa.cleanup = Mock()
    plugins['aaa'] = mock_aaa
    
    # Mock Strategy plugin
    mock_strategy = Mock()
    mock_strategy.initialize = Mock(return_value=True)
    mock_strategy.process_market_data = Mock(return_value={'signal': 'BUY', 'confidence': 0.8})
    mock_strategy.cleanup = Mock()
    plugins['strategy'] = mock_strategy
    
    # Mock Broker plugin
    mock_broker = Mock()
    mock_broker.initialize = Mock(return_value=True)
    mock_broker.execute_order = Mock(return_value={'order_id': 'ORD123', 'status': 'FILLED'})
    mock_broker.cleanup = Mock()
    plugins['broker'] = mock_broker
    
    # Mock Portfolio plugin
    mock_portfolio = Mock()
    mock_portfolio.initialize = Mock(return_value=True)
    mock_portfolio.update_position = Mock(return_value={'position': 100, 'pnl': 150.0})
    mock_portfolio.cleanup = Mock()
    plugins['portfolio'] = mock_portfolio
    
    return plugins

@pytest.fixture
def sample_market_data():
    """Create sample market data for testing."""
    return {
        'AAPL': {
            'price': 150.00,
            'volume': 1000000,
            'timestamp': 1640995200,
            'prices': [148.0, 149.0, 150.0, 151.0, 150.5] * 10  # 50 prices for SMA
        },
        'GOOGL': {
            'price': 2800.00,
            'volume': 500000,
            'timestamp': 1640995200,
            'prices': [2750.0, 2780.0, 2800.0, 2820.0, 2810.0] * 10
        }
    }

@pytest.fixture
def sample_user_data():
    """Create sample user data for testing."""
    return {
        'admin_user': {
            'username': 'admin',
            'email': 'admin@example.com',
            'role': 'admin',
            'permissions': ['read', 'write', 'delete', 'manage_users']
        },
        'trader_user': {
            'username': 'trader',
            'email': 'trader@example.com',
            'role': 'trader',
            'permissions': ['read', 'write', 'trade']
        },
        'readonly_user': {
            'username': 'readonly',
            'email': 'readonly@example.com',
            'role': 'readonly',
            'permissions': ['read']
        }
    }

@pytest.fixture
def sample_portfolio_data():
    """Create sample portfolio data for testing."""
    return {
        'portfolio_1': {
            'id': 1,
            'name': 'Growth Portfolio',
            'user_id': 'trader_user',
            'balance': 100000.0,
            'assets': ['AAPL', 'GOOGL', 'MSFT'],
            'positions': {
                'AAPL': {'quantity': 100, 'avg_price': 148.0},
                'GOOGL': {'quantity': 10, 'avg_price': 2750.0}
            }
        },
        'portfolio_2': {
            'id': 2,
            'name': 'Conservative Portfolio',
            'user_id': 'trader_user',
            'balance': 50000.0,
            'assets': ['BOND', 'CASH'],
            'positions': {
                'BOND': {'quantity': 500, 'avg_price': 98.0}
            }
        }
    }

# Test markers for categorizing tests
def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line(
        "markers", "unit: mark test as a unit test"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )
    config.addinivalue_line(
        "markers", "system: mark test as a system test"
    )
    config.addinivalue_line(
        "markers", "acceptance: mark test as an acceptance test"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers", "security: mark test as security-related"
    )
    config.addinivalue_line(
        "markers", "performance: mark test as performance-related"
    )

# Pytest collection hooks
def pytest_collection_modifyitems(config, items):
    """Modify test items during collection."""
    for item in items:
        # Add markers based on test location
        if "unit" in str(item.fspath):
            item.add_marker(pytest.mark.unit)
        elif "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
        elif "system" in str(item.fspath):
            item.add_marker(pytest.mark.system)
        elif "acceptance" in str(item.fspath):
            item.add_marker(pytest.mark.acceptance)
        
        # Add slow marker to tests that might be slow
        if any(keyword in item.name.lower() for keyword in ['performance', 'stress', 'load']):
            item.add_marker(pytest.mark.slow)
        
        # Add security marker to security tests
        if any(keyword in item.name.lower() for keyword in ['security', 'auth', 'permission']):
            item.add_marker(pytest.mark.security)
