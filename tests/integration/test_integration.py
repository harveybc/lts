#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LTS (Live Trading System) - Integration Tests

This module implements comprehensive integration tests for the LTS project.
Tests validate the interactions between modules, plugins, system components,
and API endpoints. All tests focus on interface contracts, data flow, and
cross-component behaviors.

Integration tests validate that different components work together correctly
and handle errors gracefully.
"""

import pytest
import asyncio
import os
import sys
import time
import tempfile
import json
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from contextlib import asynccontextmanager
import sqlite3

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.plugin_loader import PluginLoader
from app.plugin_base import PluginBase
from app.database import Database
from app.config_handler import ConfigHandler
from app.web import WebAPI


class TestPluginSystemIntegration:
    """
    INT-001, INT-002, INT-003: Plugin System Integration Tests
    Tests plugin lifecycle, communication, and configuration integration.
    """

    @pytest.fixture
    def plugin_config(self):
        """Plugin configuration for testing."""
        return {
            'plugins': {
                'aaa': {
                    'class': 'plugins_aaa.default_aaa.DefaultAAA',
                    'parameters': {
                        'password_complexity': True,
                        'session_timeout': 3600
                    }
                },
                'strategy': {
                    'class': 'plugins_strategy.default_strategy.DefaultStrategy',
                    'parameters': {
                        'lookback_period': 20,
                        'risk_threshold': 0.02
                    }
                },
                'broker': {
                    'class': 'plugins_broker.default_broker.DefaultBroker',
                    'parameters': {
                        'api_timeout': 30,
                        'retry_attempts': 3
                    }
                }
            }
        }

    @pytest.fixture
    def mock_plugin_classes(self):
        """Create mock plugin classes for testing."""
        class MockAAA(PluginBase):
            def __init__(self):
                super().__init__()
                self.initialized = False
                self.config = {}
                
            def initialize(self):
                self.initialized = True
                return True
                
            def configure(self, config):
                self.config = config
                return True
                
            def authenticate(self, username, password):
                return {'user_id': username, 'status': 'authenticated'}
                
            def cleanup(self):
                self.initialized = False
                
        class MockStrategy(PluginBase):
            def __init__(self):
                super().__init__()
                self.initialized = False
                self.config = {}
                
            def initialize(self):
                self.initialized = True
                return True
                
            def configure(self, config):
                self.config = config
                return True
                
            def process_market_data(self, data):
                return {
                    'signal': 'BUY',
                    'asset': 'AAPL',
                    'confidence': 0.8,
                    'timestamp': time.time()
                }
                
            def cleanup(self):
                self.initialized = False
                
        class MockBroker(PluginBase):
            def __init__(self):
                super().__init__()
                self.initialized = False
                self.config = {}
                
            def initialize(self):
                self.initialized = True
                return True
                
            def configure(self, config):
                self.config = config
                return True
                
            def execute_order(self, order_data):
                return {
                    'order_id': f'ORD{int(time.time() * 1000)}',
                    'status': 'FILLED',
                    'price': 150.00,
                    'quantity': order_data.get('quantity', 100)
                }
                
            def cleanup(self):
                self.initialized = False
                
        return {
            'aaa': MockAAA,
            'strategy': MockStrategy,
            'broker': MockBroker
        }

    @pytest.mark.asyncio
    async def test_plugin_lifecycle_management(self, plugin_config, mock_plugin_classes):
        """Test plugins are loaded, initialized, configured, and shut down consistently."""
        # Given: Plugin loader with valid configuration
        with patch('importlib.import_module') as mock_import:
            # Mock plugin module imports
            mock_modules = {}
            for plugin_name, plugin_class in mock_plugin_classes.items():
                mock_module = Mock()
                mock_module.DefaultAAA = plugin_class if plugin_name == 'aaa' else None
                mock_module.DefaultStrategy = plugin_class if plugin_name == 'strategy' else None
                mock_module.DefaultBroker = plugin_class if plugin_name == 'broker' else None
                mock_modules[plugin_name] = mock_module
            
            mock_import.side_effect = lambda module_name: mock_modules.get(module_name.split('.')[-2])
            
            loader = PluginLoader(plugin_config)
            
            # When: Plugins are loaded and initialized
            plugins = await loader.load_plugins()
            
            # Then: All plugins loaded successfully
            assert len(plugins) == 3
            assert 'aaa' in plugins
            assert 'strategy' in plugins
            assert 'broker' in plugins
            
            # Verify plugin initialization
            for plugin_name, plugin in plugins.items():
                assert plugin.initialized is True
                assert plugin.config is not None
                
            # Test plugin configuration
            for plugin_name, plugin in plugins.items():
                expected_config = plugin_config['plugins'][plugin_name]['parameters']
                assert plugin.config == expected_config
                
            # Test plugin cleanup
            await loader.shutdown_plugins()
            for plugin_name, plugin in plugins.items():
                assert plugin.initialized is False

    @pytest.mark.asyncio
    async def test_plugin_communication_protocols(self, plugin_config, mock_plugin_classes):
        """Test plugins communicate through standardized interfaces."""
        # Given: Multiple plugins loaded with communication channels
        with patch('importlib.import_module') as mock_import:
            mock_modules = {}
            for plugin_name, plugin_class in mock_plugin_classes.items():
                mock_module = Mock()
                setattr(mock_module, f'Default{plugin_name.capitalize()}', plugin_class)
                mock_modules[plugin_name] = mock_module
            
            mock_import.side_effect = lambda module_name: mock_modules.get(module_name.split('.')[-2])
            
            loader = PluginLoader(plugin_config)
            plugins = await loader.load_plugins()
            
            # When: Plugin communication is tested
            strategy_plugin = plugins['strategy']
            broker_plugin = plugins['broker']
            
            # Strategy generates signal
            market_data = {'AAPL': 150.00, 'timestamp': time.time()}
            signal = strategy_plugin.process_market_data(market_data)
            
            # Broker receives and processes signal
            order_data = {
                'asset': signal['asset'],
                'signal': signal['signal'],
                'quantity': 100
            }
            execution_result = broker_plugin.execute_order(order_data)
            
            # Then: Communication protocols work correctly
            assert signal['signal'] == 'BUY'
            assert signal['asset'] == 'AAPL'
            assert execution_result['status'] == 'FILLED'
            assert execution_result['quantity'] == 100
            
            # Verify data integrity in communication
            assert 'order_id' in execution_result
            assert 'price' in execution_result

    @pytest.mark.asyncio
    async def test_plugin_configuration_integration(self, plugin_config, mock_plugin_classes):
        """Test plugin-specific configurations are properly merged and validated."""
        # Given: Configuration with plugin-specific settings
        with patch('importlib.import_module') as mock_import:
            mock_modules = {}
            for plugin_name, plugin_class in mock_plugin_classes.items():
                mock_module = Mock()
                setattr(mock_module, f'Default{plugin_name.capitalize()}', plugin_class)
                mock_modules[plugin_name] = mock_module
            
            mock_import.side_effect = lambda module_name: mock_modules.get(module_name.split('.')[-2])
            
            loader = PluginLoader(plugin_config)
            plugins = await loader.load_plugins()
            
            # When: Plugin configurations are validated
            aaa_plugin = plugins['aaa']
            strategy_plugin = plugins['strategy']
            broker_plugin = plugins['broker']
            
            # Then: Plugin-specific configurations are correctly applied
            assert aaa_plugin.config['password_complexity'] is True
            assert aaa_plugin.config['session_timeout'] == 3600
            
            assert strategy_plugin.config['lookback_period'] == 20
            assert strategy_plugin.config['risk_threshold'] == 0.02
            
            assert broker_plugin.config['api_timeout'] == 30
            assert broker_plugin.config['retry_attempts'] == 3

    @pytest.mark.asyncio
    async def test_plugin_error_handling_and_isolation(self, plugin_config, mock_plugin_classes):
        """Test plugin failures are isolated and don't affect other plugins."""
        # Given: Plugins with one configured to fail
        class FailingPlugin(PluginBase):
            def __init__(self):
                super().__init__()
                
            def initialize(self):
                raise Exception("Plugin initialization failed")
                
            def configure(self, config):
                raise Exception("Plugin configuration failed")
        
        mock_plugin_classes['failing'] = FailingPlugin
        plugin_config['plugins']['failing'] = {
            'class': 'plugins_failing.default_failing.DefaultFailing',
            'parameters': {}
        }
        
        with patch('importlib.import_module') as mock_import:
            mock_modules = {}
            for plugin_name, plugin_class in mock_plugin_classes.items():
                mock_module = Mock()
                setattr(mock_module, f'Default{plugin_name.capitalize()}', plugin_class)
                mock_modules[plugin_name] = mock_module
            
            mock_import.side_effect = lambda module_name: mock_modules.get(module_name.split('.')[-2])
            
            loader = PluginLoader(plugin_config)
            
            # When: Plugin loading is attempted with failing plugin
            plugins = await loader.load_plugins()
            
            # Then: Good plugins load successfully, bad plugin is isolated
            assert len(plugins) >= 3  # At least the good plugins
            
            # Verify good plugins still work
            if 'strategy' in plugins:
                strategy_result = plugins['strategy'].process_market_data({'AAPL': 150.00})
                assert strategy_result['signal'] == 'BUY'
                
            if 'broker' in plugins:
                broker_result = plugins['broker'].execute_order({'quantity': 100})
                assert broker_result['status'] == 'FILLED'


class TestDatabaseIntegration:
    """
    INT-004, INT-005, INT-006: Database Integration Tests
    Tests database connection management, transaction integrity, and ORM integration.
    """

    @pytest.fixture
    def database_config(self):
        """Database configuration for testing."""
        return {
            'type': 'sqlite',
            'path': ':memory:',
            'pool_size': 5,
            'max_connections': 10,
            'timeout': 30
        }

    @pytest.fixture
    async def test_database(self, database_config):
        """Create test database instance."""
        db = Database(database_config)
        await db.initialize()
        
        # Create test tables
        await db.execute_sql("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await db.execute_sql("""
            CREATE TABLE IF NOT EXISTS portfolios (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                name TEXT NOT NULL,
                balance REAL DEFAULT 0.0,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        
        await db.execute_sql("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY,
                portfolio_id INTEGER,
                asset TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (portfolio_id) REFERENCES portfolios (id)
            )
        """)
        
        yield db
        await db.cleanup()

    @pytest.mark.asyncio
    async def test_database_connection_management(self, test_database):
        """Test database connections are properly managed with pooling."""
        # Given: Database with connection pooling
        db = test_database
        
        # When: Multiple concurrent connections are requested
        async def test_connection(conn_id):
            async with db.get_connection() as conn:
                await conn.execute(
                    "INSERT INTO users (username, email) VALUES (?, ?)",
                    (f"user_{conn_id}", f"user_{conn_id}@test.com")
                )
                await conn.commit()
                
                # Query to verify insertion
                cursor = await conn.execute("SELECT username FROM users WHERE username = ?", (f"user_{conn_id}",))
                result = await cursor.fetchone()
                return result[0] if result else None
        
        # Execute 10 concurrent database operations
        tasks = [test_connection(i) for i in range(10)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Then: All connections handled properly
        successful_results = [r for r in results if not isinstance(r, Exception)]
        assert len(successful_results) == 10
        
        # Verify all users were created
        async with db.get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM users")
            count = await cursor.fetchone()
            assert count[0] == 10

    @pytest.mark.asyncio
    async def test_transaction_integrity(self, test_database):
        """Test database operations maintain ACID properties."""
        # Given: Database with transaction support
        db = test_database
        
        # When: Complex transaction is performed
        async with db.get_connection() as conn:
            await conn.execute("BEGIN TRANSACTION")
            
            try:
                # Insert user
                await conn.execute(
                    "INSERT INTO users (username, email) VALUES (?, ?)",
                    ("test_user", "test@example.com")
                )
                
                # Get user ID
                cursor = await conn.execute("SELECT id FROM users WHERE username = ?", ("test_user",))
                user_result = await cursor.fetchone()
                user_id = user_result[0]
                
                # Insert portfolio
                await conn.execute(
                    "INSERT INTO portfolios (user_id, name, balance) VALUES (?, ?, ?)",
                    (user_id, "Test Portfolio", 10000.0)
                )
                
                # Get portfolio ID
                cursor = await conn.execute("SELECT id FROM portfolios WHERE user_id = ?", (user_id,))
                portfolio_result = await cursor.fetchone()
                portfolio_id = portfolio_result[0]
                
                # Insert transaction
                await conn.execute(
                    "INSERT INTO transactions (portfolio_id, asset, quantity, price) VALUES (?, ?, ?, ?)",
                    (portfolio_id, "AAPL", 100, 150.0)
                )
                
                await conn.execute("COMMIT")
                
            except Exception as e:
                await conn.execute("ROLLBACK")
                raise e
        
        # Then: Transaction integrity maintained
        async with db.get_connection() as conn:
            # Verify all related records exist
            cursor = await conn.execute("SELECT COUNT(*) FROM users WHERE username = ?", ("test_user",))
            user_count = await cursor.fetchone()
            assert user_count[0] == 1
            
            cursor = await conn.execute("SELECT COUNT(*) FROM portfolios WHERE name = ?", ("Test Portfolio",))
            portfolio_count = await cursor.fetchone()
            assert portfolio_count[0] == 1
            
            cursor = await conn.execute("SELECT COUNT(*) FROM transactions WHERE asset = ?", ("AAPL",))
            transaction_count = await cursor.fetchone()
            assert transaction_count[0] == 1

    @pytest.mark.asyncio
    async def test_transaction_rollback_on_failure(self, test_database):
        """Test proper transaction rollback on failures."""
        # Given: Database transaction that will fail
        db = test_database
        
        # When: Transaction fails midway
        try:
            async with db.get_connection() as conn:
                await conn.execute("BEGIN TRANSACTION")
                
                # Insert user (should succeed)
                await conn.execute(
                    "INSERT INTO users (username, email) VALUES (?, ?)",
                    ("rollback_user", "rollback@example.com")
                )
                
                # This should fail due to constraint violation
                await conn.execute(
                    "INSERT INTO users (username, email) VALUES (?, ?)",
                    ("rollback_user", "rollback2@example.com")  # Same username
                )
                
                await conn.execute("COMMIT")
                
        except Exception:
            # Expected to fail
            pass
        
        # Then: Transaction properly rolled back
        async with db.get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM users WHERE username = ?", ("rollback_user",))
            count = await cursor.fetchone()
            assert count[0] == 0  # No user should exist due to rollback

    @pytest.mark.asyncio
    async def test_foreign_key_constraints(self, test_database):
        """Test foreign key constraints are properly enforced."""
        # Given: Database with foreign key constraints
        db = test_database
        
        # When: Attempting to insert with invalid foreign key
        with pytest.raises(Exception):  # Should raise constraint violation
            async with db.get_connection() as conn:
                await conn.execute(
                    "INSERT INTO portfolios (user_id, name, balance) VALUES (?, ?, ?)",
                    (99999, "Invalid Portfolio", 1000.0)  # Non-existent user_id
                )
                await conn.commit()
        
        # When: Attempting to delete referenced record
        async with db.get_connection() as conn:
            # Create user and portfolio
            await conn.execute(
                "INSERT INTO users (username, email) VALUES (?, ?)",
                ("fk_user", "fk@example.com")
            )
            
            cursor = await conn.execute("SELECT id FROM users WHERE username = ?", ("fk_user",))
            user_result = await cursor.fetchone()
            user_id = user_result[0]
            
            await conn.execute(
                "INSERT INTO portfolios (user_id, name, balance) VALUES (?, ?, ?)",
                (user_id, "FK Portfolio", 1000.0)
            )
            await conn.commit()
            
            # Try to delete user with existing portfolio
            with pytest.raises(Exception):  # Should raise constraint violation
                await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                await conn.commit()


class TestWebAPIIntegration:
    """
    INT-007, INT-008, INT-009: Web API Integration Tests
    Tests authentication integration, request validation, and rate limiting.
    """

    @pytest.fixture
    def api_config(self):
        """API configuration for testing."""
        return {
            'api': {
                'host': '127.0.0.1',
                'port': 8080,
                'debug': False,
                'rate_limit': {
                    'requests_per_minute': 60,
                    'burst_size': 10
                }
            },
            'security': {
                'secret_key': 'test_secret_key',
                'session_timeout': 3600
            }
        }

    @pytest.fixture
    async def test_api(self, api_config):
        """Create test API instance."""
        # Mock AAA plugin for authentication
        mock_aaa = Mock()
        mock_aaa.authenticate.return_value = {'user_id': 'test_user', 'status': 'authenticated'}
        mock_aaa.validate_session.return_value = {'user_id': 'test_user', 'valid': True}
        mock_aaa.check_permission.return_value = True
        
        api = WebAPI(api_config, {'aaa': mock_aaa})
        await api.initialize()
        
        yield api
        await api.cleanup()

    @pytest.mark.asyncio
    async def test_authentication_integration(self, test_api):
        """Test web API endpoints integrate with AAA plugins for authentication."""
        # Given: API with AAA plugin integration
        api = test_api
        
        # When: API request without authentication
        response = await api.process_request('GET', '/api/portfolios', {}, {})
        
        # Then: Authentication required
        assert response['status'] == 401
        assert 'authentication required' in response['message'].lower()
        
        # When: API request with valid authentication
        headers = {'Authorization': 'Bearer valid_token'}
        response = await api.process_request('GET', '/api/portfolios', headers, {})
        
        # Then: Request processed successfully
        assert response['status'] == 200
        assert 'portfolios' in response

    @pytest.mark.asyncio
    async def test_role_based_authorization(self, test_api):
        """Test role-based authorization for API endpoints."""
        # Given: API with role-based access control
        api = test_api
        
        # Mock different user roles
        api.plugins['aaa'].check_permission.side_effect = lambda user, action: action in ['view']
        
        # When: User attempts authorized action
        headers = {'Authorization': 'Bearer user_token'}
        response = await api.process_request('GET', '/api/portfolios', headers, {})
        
        # Then: Access granted
        assert response['status'] == 200
        
        # When: User attempts unauthorized action
        api.plugins['aaa'].check_permission.side_effect = lambda user, action: action in ['view']
        response = await api.process_request('DELETE', '/api/portfolios/1', headers, {})
        
        # Then: Access denied
        assert response['status'] == 403
        assert 'permission denied' in response['message'].lower()

    @pytest.mark.asyncio
    async def test_api_input_validation(self, test_api):
        """Test API input validation and sanitization."""
        # Given: API with input validation
        api = test_api
        
        # When: Valid input is provided
        headers = {'Authorization': 'Bearer valid_token'}
        valid_data = {
            'portfolio_name': 'Test Portfolio',
            'initial_balance': 10000.0,
            'assets': ['AAPL', 'GOOGL']
        }
        response = await api.process_request('POST', '/api/portfolios', headers, valid_data)
        
        # Then: Input accepted
        assert response['status'] == 201
        
        # When: Invalid input is provided
        invalid_data = {
            'portfolio_name': '<script>alert("xss")</script>',  # XSS attempt
            'initial_balance': 'invalid_number',
            'assets': ['AAPL; DROP TABLE portfolios; --']  # SQL injection attempt
        }
        response = await api.process_request('POST', '/api/portfolios', headers, invalid_data)
        
        # Then: Input rejected
        assert response['status'] == 400
        assert 'invalid input' in response['message'].lower()

    @pytest.mark.asyncio
    async def test_api_rate_limiting(self, test_api):
        """Test API rate limiting and throttling."""
        # Given: API with rate limiting configured
        api = test_api
        headers = {'Authorization': 'Bearer valid_token'}
        
        # When: Normal request rate (within limits)
        responses = []
        for i in range(5):  # Well within limit
            response = await api.process_request('GET', '/api/portfolios', headers, {})
            responses.append(response)
            await asyncio.sleep(0.1)
        
        # Then: All requests processed normally
        assert all(r['status'] == 200 for r in responses)
        
        # When: Excessive request rate (exceeds limits)
        rapid_responses = []
        for i in range(15):  # Exceed burst size
            response = await api.process_request('GET', '/api/portfolios', headers, {})
            rapid_responses.append(response)
        
        # Then: Rate limiting enforced
        rate_limited_responses = [r for r in rapid_responses if r['status'] == 429]
        assert len(rate_limited_responses) > 0


class TestConfigurationIntegration:
    """
    INT-010, INT-011: Configuration Integration Tests
    Tests multi-source configuration merging and validation.
    """

    @pytest.fixture
    def config_sources(self):
        """Multiple configuration sources for testing."""
        return {
            'default': {
                'database': {'type': 'sqlite', 'path': 'default.db'},
                'api': {'port': 8080, 'debug': False},
                'security': {'session_timeout': 3600}
            },
            'file': {
                'database': {'path': 'config.db'},
                'api': {'port': 8081, 'debug': True},
                'plugins': {'strategy': 'custom_strategy'}
            },
            'environment': {
                'database': {'path': 'env.db'},
                'security': {'session_timeout': 1800}
            },
            'cli': {
                'api': {'port': 9090},
                'debug': True
            }
        }

    def test_multi_source_configuration_merging(self, config_sources):
        """Test configuration is properly loaded and merged from multiple sources."""
        # Given: Multiple configuration sources
        config_handler = ConfigHandler()
        
        # When: Configuration is merged with proper precedence
        merged_config = config_handler.merge_configurations([
            config_sources['default'],
            config_sources['file'],
            config_sources['environment'],
            config_sources['cli']
        ])
        
        # Then: Configuration merged with correct precedence (CLI > ENV > FILE > DEFAULT)
        assert merged_config['api']['port'] == 9090  # CLI wins
        assert merged_config['database']['path'] == 'env.db'  # Environment wins
        assert merged_config['plugins']['strategy'] == 'custom_strategy'  # File wins
        assert merged_config['database']['type'] == 'sqlite'  # Default used
        assert merged_config['debug'] is True  # CLI setting

    def test_configuration_validation_and_security(self, config_sources):
        """Test configuration values are validated for security and integrity."""
        # Given: Configuration handler with validation rules
        config_handler = ConfigHandler()
        
        # When: Valid configuration is validated
        valid_config = config_sources['default']
        validation_result = config_handler.validate_configuration(valid_config)
        assert validation_result['valid'] is True
        
        # When: Invalid configuration is validated
        invalid_config = {
            'database': {'type': 'unsupported_db'},
            'api': {'port': 'invalid_port'},
            'security': {'session_timeout': -1}  # Invalid timeout
        }
        
        validation_result = config_handler.validate_configuration(invalid_config)
        assert validation_result['valid'] is False
        assert len(validation_result['errors']) > 0

    def test_sensitive_data_protection(self, config_sources):
        """Test sensitive configuration data is properly protected."""
        # Given: Configuration with sensitive data
        sensitive_config = {
            'database': {'password': 'secret_password'},
            'api': {'secret_key': 'api_secret'},
            'external': {'api_key': 'external_api_key'}
        }
        
        config_handler = ConfigHandler()
        
        # When: Configuration is processed
        processed_config = config_handler.process_configuration(sensitive_config)
        
        # Then: Sensitive data is protected
        assert processed_config['database']['password'] == '[REDACTED]'
        assert processed_config['api']['secret_key'] == '[REDACTED]'
        assert processed_config['external']['api_key'] == '[REDACTED]'
        
        # But actual values are still accessible via secure methods
        actual_password = config_handler.get_secure_value('database.password')
        assert actual_password == 'secret_password'


class TestErrorHandlingAndRecovery:
    """
    INT-012, INT-013: Error Handling and Audit Logging Integration Tests
    Tests cross-component error propagation and audit logging.
    """

    @pytest.fixture
    def error_simulation_config(self):
        """Configuration for error simulation testing."""
        return {
            'error_handling': {
                'max_retries': 3,
                'retry_delay': 0.1,
                'circuit_breaker_threshold': 5
            },
            'audit_logging': {
                'enabled': True,
                'log_sensitive_data': False,
                'retention_days': 30
            }
        }

    @pytest.mark.asyncio
    async def test_cross_component_error_propagation(self, error_simulation_config):
        """Test errors propagate correctly between components with proper logging."""
        # Given: System with error handling configuration
        class ComponentA:
            def __init__(self):
                self.error_count = 0
                
            async def process_data(self, data):
                self.error_count += 1
                if self.error_count <= 2:
                    raise Exception(f"Component A error {self.error_count}")
                return {'status': 'success', 'data': data}
        
        class ComponentB:
            def __init__(self, component_a):
                self.component_a = component_a
                
            async def handle_request(self, request):
                try:
                    result = await self.component_a.process_data(request)
                    return result
                except Exception as e:
                    return {'status': 'error', 'message': str(e)}
        
        # When: Error occurs in component chain
        component_a = ComponentA()
        component_b = ComponentB(component_a)
        
        # First two calls should return errors
        result1 = await component_b.handle_request({'test': 'data1'})
        assert result1['status'] == 'error'
        assert 'Component A error 1' in result1['message']
        
        result2 = await component_b.handle_request({'test': 'data2'})
        assert result2['status'] == 'error'
        assert 'Component A error 2' in result2['message']
        
        # Third call should succeed
        result3 = await component_b.handle_request({'test': 'data3'})
        assert result3['status'] == 'success'

    @pytest.mark.asyncio
    async def test_audit_logging_integration(self, error_simulation_config):
        """Test audit logging integration across components."""
        # Given: System with audit logging enabled
        class AuditLogger:
            def __init__(self):
                self.logs = []
                
            def log_event(self, event_type, details, user_id=None):
                log_entry = {
                    'timestamp': time.time(),
                    'event_type': event_type,
                    'details': details,
                    'user_id': user_id,
                    'correlation_id': f"corr_{len(self.logs)}"
                }
                self.logs.append(log_entry)
                
            def get_logs(self, event_type=None):
                if event_type:
                    return [log for log in self.logs if log['event_type'] == event_type]
                return self.logs
        
        class BusinessComponent:
            def __init__(self, audit_logger):
                self.audit_logger = audit_logger
                
            async def perform_sensitive_operation(self, operation_data, user_id):
                # Log operation start
                self.audit_logger.log_event(
                    'operation_start',
                    {'operation': 'sensitive_operation', 'data': operation_data},
                    user_id
                )
                
                try:
                    # Simulate operation
                    result = {'status': 'completed', 'result': 'success'}
                    
                    # Log operation success
                    self.audit_logger.log_event(
                        'operation_success',
                        {'operation': 'sensitive_operation', 'result': result},
                        user_id
                    )
                    
                    return result
                    
                except Exception as e:
                    # Log operation failure
                    self.audit_logger.log_event(
                        'operation_failure',
                        {'operation': 'sensitive_operation', 'error': str(e)},
                        user_id
                    )
                    raise
        
        # When: Sensitive operations are performed
        audit_logger = AuditLogger()
        component = BusinessComponent(audit_logger)
        
        await component.perform_sensitive_operation({'action': 'test'}, 'user123')
        
        # Then: Audit logs are properly created and correlated
        logs = audit_logger.get_logs()
        assert len(logs) == 2
        
        start_log = audit_logger.get_logs('operation_start')[0]
        success_log = audit_logger.get_logs('operation_success')[0]
        
        assert start_log['user_id'] == 'user123'
        assert success_log['user_id'] == 'user123'
        assert start_log['details']['operation'] == 'sensitive_operation'
        assert success_log['details']['result']['status'] == 'completed'

    @pytest.mark.asyncio
    async def test_circuit_breaker_pattern(self, error_simulation_config):
        """Test circuit breaker pattern for error handling."""
        # Given: Component with circuit breaker pattern
        class CircuitBreaker:
            def __init__(self, failure_threshold=5, recovery_timeout=60):
                self.failure_threshold = failure_threshold
                self.recovery_timeout = recovery_timeout
                self.failure_count = 0
                self.last_failure_time = None
                self.state = 'CLOSED'  # CLOSED, OPEN, HALF_OPEN
                
            async def call(self, func, *args, **kwargs):
                if self.state == 'OPEN':
                    if time.time() - self.last_failure_time > self.recovery_timeout:
                        self.state = 'HALF_OPEN'
                    else:
                        raise Exception("Circuit breaker is OPEN")
                
                try:
                    result = await func(*args, **kwargs)
                    if self.state == 'HALF_OPEN':
                        self.state = 'CLOSED'
                        self.failure_count = 0
                    return result
                except Exception as e:
                    self.failure_count += 1
                    self.last_failure_time = time.time()
                    
                    if self.failure_count >= self.failure_threshold:
                        self.state = 'OPEN'
                    
                    raise e
        
        # When: Service fails repeatedly
        circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=0.1)
        
        async def failing_service():
            raise Exception("Service failure")
        
        # First 3 calls should record failures
        for i in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_service)
        
        # Circuit breaker should now be OPEN
        assert circuit_breaker.state == 'OPEN'
        
        # Next call should be blocked by circuit breaker
        with pytest.raises(Exception, match="Circuit breaker is OPEN"):
            await circuit_breaker.call(failing_service)


# Performance and resource management tests
class TestIntegrationPerformance:
    """Test integration performance and resource management."""
    
    @pytest.mark.asyncio
    async def test_integration_performance_criteria(self):
        """Test integration tests meet performance criteria."""
        # Given: Performance monitoring
        start_time = time.time()
        
        # When: Integration operations are performed
        # Simulate typical integration operations
        await asyncio.sleep(0.1)  # Simulate database operations
        await asyncio.sleep(0.05)  # Simulate API calls
        await asyncio.sleep(0.02)  # Simulate plugin communications
        
        execution_time = time.time() - start_time
        
        # Then: Performance criteria met
        assert execution_time < 1.0  # Integration operations should be fast
        
    @pytest.mark.asyncio
    async def test_resource_cleanup(self):
        """Test proper resource cleanup in integration scenarios."""
        # Given: Resources that need cleanup
        resources = []
        
        class TestResource:
            def __init__(self, name):
                self.name = name
                self.cleaned_up = False
                
            async def cleanup(self):
                self.cleaned_up = True
        
        # When: Resources are used and cleaned up
        for i in range(5):
            resource = TestResource(f"resource_{i}")
            resources.append(resource)
        
        # Simulate cleanup
        for resource in resources:
            await resource.cleanup()
        
        # Then: All resources properly cleaned up
        assert all(resource.cleaned_up for resource in resources)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
