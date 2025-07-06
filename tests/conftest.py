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
from unittest.mock import Mock, MagicMock
from app.config_handler import ConfigHandler
from app.database import Database, db_session, Base

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="function")
def temp_database():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as temp_file:
        db_path = temp_file.name
    
    yield db_path
    
    # Cleanup
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass

@pytest.fixture(scope="function")
async def fresh_db(temp_database):
    """Create a fresh database for each test function."""
    db = Database(db_path=f"sqlite:///{temp_database}")
    await db.initialize()
    yield db
    await db.cleanup()

@pytest.fixture
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

@pytest.fixture
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
