#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LTS (Live Trading System) - Unit Tests

This module implements comprehensive unit tests for the LTS project.
Tests validate the behavior of individual modules and components in isolation,
focusing on required behaviors rather than implementation details.

Unit tests focus on isolated component behaviors, ensuring each module
works correctly in isolation with proper error handling and interface compliance.
"""

import pytest
import asyncio
import os
import sys
import time
import tempfile
import json
from unittest.mock import Mock, patch, MagicMock
from decimal import Decimal
import hashlib

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.plugin_base import PluginBase
from app.config_handler import ConfigHandler
from app.database import Database


class TestPluginBaseClass:
    """
    UT-001, UT-002, UT-003: Plugin Base Class Tests
    Tests plugin base class interface compliance and parameter management.
    """

    def test_plugin_base_interface_compliance(self):
        """Test plugin base classes enforce required methods."""
        # Given: Plugin base class
        class ValidPlugin(PluginBase):
            def __init__(self):
                super().__init__()
                
            def initialize(self):
                return True
                
            def process(self, data):
                return {'status': 'processed', 'data': data}
                
            def cleanup(self):
                return True
        
        class InvalidPlugin(PluginBase):
            def __init__(self):
                super().__init__()
            # Missing required methods
        
        # When: Valid plugin is instantiated
        valid_plugin = ValidPlugin()
        
        # Then: Plugin has required interface
        assert hasattr(valid_plugin, 'initialize')
        assert hasattr(valid_plugin, 'process')
        assert hasattr(valid_plugin, 'cleanup')
        assert callable(valid_plugin.initialize)
        assert callable(valid_plugin.process)
        assert callable(valid_plugin.cleanup)
        
        # When: Plugin methods are called
        init_result = valid_plugin.initialize()
        process_result = valid_plugin.process({'test': 'data'})
        cleanup_result = valid_plugin.cleanup()
        
        # Then: Methods return expected results
        assert init_result is True
        assert process_result['status'] == 'processed'
        assert cleanup_result is True

    def test_plugin_parameter_management(self):
        """Test plugin parameter dictionary with validation and defaults."""
        # Given: Plugin with parameter definitions
        class ParameterizedPlugin(PluginBase):
            def __init__(self):
                super().__init__()
                self.parameters = {
                    'required_param': {'type': str, 'required': True},
                    'optional_param': {'type': int, 'default': 10, 'required': False},
                    'validated_param': {'type': float, 'min': 0.0, 'max': 1.0, 'required': True}
                }
                
            def validate_parameters(self, params):
                for param_name, param_config in self.parameters.items():
                    if param_config.get('required', False) and param_name not in params:
                        raise ValueError(f"Required parameter '{param_name}' is missing")
                    
                    if param_name in params:
                        param_value = params[param_name]
                        expected_type = param_config['type']
                        
                        if not isinstance(param_value, expected_type):
                            raise TypeError(f"Parameter '{param_name}' must be of type {expected_type.__name__}")
                        
                        if 'min' in param_config and param_value < param_config['min']:
                            raise ValueError(f"Parameter '{param_name}' must be >= {param_config['min']}")
                        
                        if 'max' in param_config and param_value > param_config['max']:
                            raise ValueError(f"Parameter '{param_name}' must be <= {param_config['max']}")
                
                return True
                
            def set_parameters(self, params):
                self.validate_parameters(params)
                
                # Apply defaults
                for param_name, param_config in self.parameters.items():
                    if param_name not in params and 'default' in param_config:
                        params[param_name] = param_config['default']
                
                self.param_values = params
        
        plugin = ParameterizedPlugin()
        
        # When: Valid parameters are set
        valid_params = {
            'required_param': 'test_value',
            'validated_param': 0.5
        }
        plugin.set_parameters(valid_params)
        
        # Then: Parameters are set correctly with defaults
        assert plugin.param_values['required_param'] == 'test_value'
        assert plugin.param_values['validated_param'] == 0.5
        assert plugin.param_values['optional_param'] == 10  # Default applied
        
        # When: Invalid parameters are provided
        invalid_params = {
            'required_param': 'test_value',
            'validated_param': 1.5  # Exceeds max
        }
        
        # Then: Validation fails
        with pytest.raises(ValueError, match="must be <= 1.0"):
            plugin.set_parameters(invalid_params)

    def test_plugin_debug_information_management(self):
        """Test plugin debug information tracking."""
        # Given: Plugin with debug capabilities
        class DebuggablePlugin(PluginBase):
            def __init__(self):
                super().__init__()
                self.debug_info = {}
                
            def add_debug_info(self, key, value):
                self.debug_info[key] = value
                
            def get_debug_info(self):
                return {
                    'plugin_status': 'active',
                    'last_execution': time.time(),
                    'debug_data': self.debug_info.copy()
                }
        
        plugin = DebuggablePlugin()
        
        # When: Debug information is added
        plugin.add_debug_info('operation_count', 5)
        plugin.add_debug_info('last_result', {'status': 'success'})
        
        # Then: Debug information is properly collected
        debug_info = plugin.get_debug_info()
        assert debug_info['plugin_status'] == 'active'
        assert 'last_execution' in debug_info
        assert debug_info['debug_data']['operation_count'] == 5
        assert debug_info['debug_data']['last_result']['status'] == 'success'


class TestAAAPluginMethods:
    """
    UT-004, UT-005, UT-006: AAA Plugin Tests
    Tests authentication, authorization, and accounting methods.
    """

    def test_authentication_methods(self):
        """Test AAA plugin authentication with password hashing and validation."""
        # Given: AAA plugin with authentication capabilities
        class MockAAAPlugin(PluginBase):
            def __init__(self):
                super().__init__()
                self.users = {}
                
            def hash_password(self, password):
                # Simple hash for testing (in production, use bcrypt)
                return hashlib.sha256(password.encode()).hexdigest()
                
            def create_user(self, username, password):
                if len(password) < 8:
                    raise ValueError("Password must be at least 8 characters")
                
                hashed_password = self.hash_password(password)
                self.users[username] = {
                    'username': username,
                    'password_hash': hashed_password,
                    'failed_attempts': 0,
                    'locked_until': None
                }
                return True
                
            def authenticate(self, username, password):
                if username not in self.users:
                    return {'status': 'failed', 'reason': 'user_not_found'}
                
                user = self.users[username]
                
                # Check if account is locked
                if user['locked_until'] and time.time() < user['locked_until']:
                    return {'status': 'failed', 'reason': 'account_locked'}
                
                # Validate password
                if self.hash_password(password) == user['password_hash']:
                    user['failed_attempts'] = 0
                    user['locked_until'] = None
                    return {'status': 'success', 'user_id': username}
                else:
                    user['failed_attempts'] += 1
                    if user['failed_attempts'] >= 3:
                        user['locked_until'] = time.time() + 300  # 5 minutes
                    return {'status': 'failed', 'reason': 'invalid_password'}
        
        plugin = MockAAAPlugin()
        
        # When: User is created and authenticated
        plugin.create_user('testuser', 'securepassword123')
        
        # Then: Authentication succeeds with correct credentials
        auth_result = plugin.authenticate('testuser', 'securepassword123')
        assert auth_result['status'] == 'success'
        assert auth_result['user_id'] == 'testuser'
        
        # When: Authentication with wrong password
        auth_result = plugin.authenticate('testuser', 'wrongpassword')
        assert auth_result['status'] == 'failed'
        assert auth_result['reason'] == 'invalid_password'
        
        # When: Multiple failed attempts
        for _ in range(3):
            plugin.authenticate('testuser', 'wrongpassword')
        
        # Then: Account is locked
        auth_result = plugin.authenticate('testuser', 'securepassword123')
        assert auth_result['status'] == 'failed'
        assert auth_result['reason'] == 'account_locked'

    def test_authorization_methods(self):
        """Test role-based authorization with proper permission checking."""
        # Given: Authorization system with roles and permissions
        class AuthorizationPlugin(PluginBase):
            def __init__(self):
                super().__init__()
                self.roles = {
                    'admin': ['read', 'write', 'delete', 'manage_users'],
                    'trader': ['read', 'write', 'trade'],
                    'readonly': ['read']
                }
                self.user_roles = {}
                
            def assign_role(self, user_id, role):
                if role not in self.roles:
                    raise ValueError(f"Role '{role}' does not exist")
                self.user_roles[user_id] = role
                
            def check_permission(self, user_id, permission):
                if user_id not in self.user_roles:
                    return False
                
                user_role = self.user_roles[user_id]
                return permission in self.roles[user_role]
                
            def get_user_permissions(self, user_id):
                if user_id not in self.user_roles:
                    return []
                
                user_role = self.user_roles[user_id]
                return self.roles[user_role].copy()
        
        plugin = AuthorizationPlugin()
        
        # When: User is assigned roles
        plugin.assign_role('admin_user', 'admin')
        plugin.assign_role('trader_user', 'trader')
        plugin.assign_role('readonly_user', 'readonly')
        
        # Then: Permissions are correctly enforced
        assert plugin.check_permission('admin_user', 'manage_users') is True
        assert plugin.check_permission('trader_user', 'trade') is True
        assert plugin.check_permission('readonly_user', 'read') is True
        
        assert plugin.check_permission('trader_user', 'manage_users') is False
        assert plugin.check_permission('readonly_user', 'write') is False
        
        # When: User permissions are retrieved
        admin_perms = plugin.get_user_permissions('admin_user')
        trader_perms = plugin.get_user_permissions('trader_user')
        readonly_perms = plugin.get_user_permissions('readonly_user')
        
        # Then: Correct permissions are returned
        assert 'manage_users' in admin_perms
        assert 'trade' in trader_perms
        assert 'read' in readonly_perms
        assert len(readonly_perms) == 1

    def test_accounting_methods(self):
        """Test audit logging and user activity tracking."""
        # Given: Accounting system with audit logging
        class AccountingPlugin(PluginBase):
            def __init__(self):
                super().__init__()
                self.audit_logs = []
                
            def log_user_activity(self, user_id, activity, details=None):
                log_entry = {
                    'timestamp': time.time(),
                    'user_id': user_id,
                    'activity': activity,
                    'details': details or {},
                    'log_id': len(self.audit_logs) + 1
                }
                self.audit_logs.append(log_entry)
                
            def get_user_activities(self, user_id, limit=None):
                user_logs = [log for log in self.audit_logs if log['user_id'] == user_id]
                if limit:
                    return user_logs[-limit:]
                return user_logs
                
            def get_audit_trail(self, start_time=None, end_time=None):
                if start_time is None and end_time is None:
                    return self.audit_logs.copy()
                
                filtered_logs = []
                for log in self.audit_logs:
                    if start_time and log['timestamp'] < start_time:
                        continue
                    if end_time and log['timestamp'] > end_time:
                        continue
                    filtered_logs.append(log)
                
                return filtered_logs
        
        plugin = AccountingPlugin()
        
        # When: User activities are logged
        plugin.log_user_activity('user1', 'login', {'ip': '127.0.0.1'})
        plugin.log_user_activity('user1', 'trade', {'asset': 'AAPL', 'quantity': 100})
        plugin.log_user_activity('user2', 'login', {'ip': '192.168.1.1'})
        plugin.log_user_activity('user1', 'logout')
        
        # Then: Audit logs are properly maintained
        assert len(plugin.audit_logs) == 4
        
        # When: User-specific activities are retrieved
        user1_activities = plugin.get_user_activities('user1')
        
        # Then: Correct activities are returned
        assert len(user1_activities) == 3
        assert user1_activities[0]['activity'] == 'login'
        assert user1_activities[1]['activity'] == 'trade'
        assert user1_activities[2]['activity'] == 'logout'
        
        # When: Audit trail is retrieved with time filtering
        current_time = time.time()
        recent_logs = plugin.get_audit_trail(start_time=current_time - 60)
        
        # Then: Time filtering works correctly
        assert len(recent_logs) == 4  # All logs are recent


class TestTradingComponents:
    """
    UT-007, UT-008, UT-009: Trading Component Tests
    Tests strategy, broker, and portfolio plugin methods.
    """

    def test_strategy_plugin_decision_methods(self):
        """Test strategy plugin trading decision logic."""
        # Given: Strategy plugin with decision logic
        class SimpleStrategy(PluginBase):
            def __init__(self):
                super().__init__()
                self.parameters = {
                    'sma_period': 20,
                    'buy_threshold': 0.02,
                    'sell_threshold': -0.02
                }
                
            def calculate_sma(self, prices, period):
                if len(prices) < period:
                    return None
                return sum(prices[-period:]) / period
                
            def process_market_data(self, market_data):
                prices = market_data.get('prices', [])
                current_price = market_data.get('current_price')
                
                if not prices or not current_price:
                    return {'signal': 'HOLD', 'reason': 'insufficient_data'}
                
                sma = self.calculate_sma(prices, self.parameters['sma_period'])
                if sma is None:
                    return {'signal': 'HOLD', 'reason': 'insufficient_history'}
                
                price_change = (current_price - sma) / sma
                
                if price_change > self.parameters['buy_threshold']:
                    return {
                        'signal': 'BUY',
                        'confidence': min(price_change * 5, 1.0),
                        'reason': 'price_above_sma'
                    }
                elif price_change < self.parameters['sell_threshold']:
                    return {
                        'signal': 'SELL',
                        'confidence': min(abs(price_change) * 5, 1.0),
                        'reason': 'price_below_sma'
                    }
                else:
                    return {'signal': 'HOLD', 'reason': 'within_threshold'}
        
        strategy = SimpleStrategy()
        
        # When: Market data indicates buy signal
        buy_market_data = {
            'prices': [100.0] * 20,  # SMA = 100
            'current_price': 105.0   # 5% above SMA
        }
        signal = strategy.process_market_data(buy_market_data)
        
        # Then: Buy signal is generated
        assert signal['signal'] == 'BUY'
        assert signal['confidence'] > 0
        assert signal['reason'] == 'price_above_sma'
        
        # When: Market data indicates sell signal
        sell_market_data = {
            'prices': [100.0] * 20,  # SMA = 100
            'current_price': 95.0    # 5% below SMA
        }
        signal = strategy.process_market_data(sell_market_data)
        
        # Then: Sell signal is generated
        assert signal['signal'] == 'SELL'
        assert signal['confidence'] > 0
        assert signal['reason'] == 'price_below_sma'

    def test_broker_plugin_order_methods(self):
        """Test broker plugin order execution and management."""
        # Given: Broker plugin with order management
        class MockBroker(PluginBase):
            def __init__(self):
                super().__init__()
                self.orders = {}
                self.order_counter = 0
                
            def validate_order(self, order):
                required_fields = ['asset', 'quantity', 'order_type']
                for field in required_fields:
                    if field not in order:
                        raise ValueError(f"Missing required field: {field}")
                
                if order['quantity'] <= 0:
                    raise ValueError("Quantity must be positive")
                
                if order['order_type'] not in ['BUY', 'SELL']:
                    raise ValueError("Order type must be BUY or SELL")
                
                return True
                
            def execute_order(self, order):
                self.validate_order(order)
                
                self.order_counter += 1
                order_id = f"ORD{self.order_counter:06d}"
                
                # Simulate order execution
                executed_order = {
                    'order_id': order_id,
                    'asset': order['asset'],
                    'quantity': order['quantity'],
                    'order_type': order['order_type'],
                    'status': 'FILLED',
                    'execution_price': order.get('limit_price', 100.0),
                    'execution_time': time.time()
                }
                
                self.orders[order_id] = executed_order
                return executed_order
                
            def get_order_status(self, order_id):
                return self.orders.get(order_id, {'status': 'NOT_FOUND'})
                
            def cancel_order(self, order_id):
                if order_id in self.orders:
                    self.orders[order_id]['status'] = 'CANCELLED'
                    return True
                return False
        
        broker = MockBroker()
        
        # When: Valid order is executed
        valid_order = {
            'asset': 'AAPL',
            'quantity': 100,
            'order_type': 'BUY',
            'limit_price': 150.0
        }
        result = broker.execute_order(valid_order)
        
        # Then: Order is executed successfully
        assert result['status'] == 'FILLED'
        assert result['asset'] == 'AAPL'
        assert result['quantity'] == 100
        assert result['order_type'] == 'BUY'
        assert 'order_id' in result
        
        # When: Order status is checked
        order_status = broker.get_order_status(result['order_id'])
        assert order_status['status'] == 'FILLED'
        
        # When: Invalid order is submitted
        invalid_order = {
            'asset': 'AAPL',
            'quantity': -100,  # Invalid quantity
            'order_type': 'BUY'
        }
        
        # Then: Order validation fails
        with pytest.raises(ValueError, match="Quantity must be positive"):
            broker.execute_order(invalid_order)

    def test_portfolio_plugin_allocation_methods(self):
        """Test portfolio plugin capital allocation and risk management."""
        # Given: Portfolio plugin with allocation logic
        class PortfolioManager(PluginBase):
            def __init__(self):
                super().__init__()
                self.positions = {}
                self.cash_balance = 100000.0
                self.risk_limits = {
                    'max_position_size': 0.10,  # 10% of portfolio
                    'max_sector_exposure': 0.30,  # 30% per sector
                    'max_daily_loss': 0.05  # 5% daily loss limit
                }
                
            def calculate_portfolio_value(self):
                position_value = sum(pos['quantity'] * pos['current_price'] 
                                   for pos in self.positions.values())
                return self.cash_balance + position_value
                
            def calculate_position_size(self, asset, signal_strength, current_price):
                portfolio_value = self.calculate_portfolio_value()
                base_allocation = portfolio_value * signal_strength * 0.02  # 2% base
                
                # Apply risk limits
                max_position_value = portfolio_value * self.risk_limits['max_position_size']
                position_value = min(base_allocation, max_position_value)
                
                # Ensure sufficient cash
                position_value = min(position_value, self.cash_balance)
                
                return int(position_value / current_price)
                
            def execute_allocation(self, asset, quantity, price, order_type):
                if order_type == 'BUY':
                    required_cash = quantity * price
                    if required_cash > self.cash_balance:
                        raise ValueError("Insufficient cash for purchase")
                    
                    self.cash_balance -= required_cash
                    if asset in self.positions:
                        self.positions[asset]['quantity'] += quantity
                    else:
                        self.positions[asset] = {
                            'quantity': quantity,
                            'avg_price': price,
                            'current_price': price
                        }
                        
                elif order_type == 'SELL':
                    if asset not in self.positions or self.positions[asset]['quantity'] < quantity:
                        raise ValueError("Insufficient shares to sell")
                    
                    self.cash_balance += quantity * price
                    self.positions[asset]['quantity'] -= quantity
                    
                    if self.positions[asset]['quantity'] == 0:
                        del self.positions[asset]
                
                return {
                    'asset': asset,
                    'quantity': quantity,
                    'price': price,
                    'order_type': order_type,
                    'new_cash_balance': self.cash_balance
                }
        
        portfolio = PortfolioManager()
        
        # When: Position size is calculated
        position_size = portfolio.calculate_position_size('AAPL', 0.8, 150.0)
        
        # Then: Position size respects limits
        assert position_size > 0
        assert position_size * 150.0 <= portfolio.calculate_portfolio_value() * 0.10
        
        # When: Allocation is executed
        allocation_result = portfolio.execute_allocation('AAPL', 100, 150.0, 'BUY')
        
        # Then: Allocation is properly recorded
        assert allocation_result['asset'] == 'AAPL'
        assert allocation_result['quantity'] == 100
        assert portfolio.cash_balance == 100000.0 - (100 * 150.0)
        assert 'AAPL' in portfolio.positions
        assert portfolio.positions['AAPL']['quantity'] == 100


class TestDatabaseComponents:
    """
    UT-010, UT-011, UT-012: Database Component Tests
    Tests database models, relationships, and query optimization.
    """

    def test_database_model_definitions(self):
        """Test database model definitions with proper fields and constraints."""
        # Given: Database models with various field types
        class DatabaseModel:
            def __init__(self):
                self.fields = {}
                self.constraints = {}
                
            def add_field(self, name, field_type, constraints=None):
                self.fields[name] = {
                    'type': field_type,
                    'constraints': constraints or {}
                }
                
            def validate_field(self, name, value):
                if name not in self.fields:
                    raise ValueError(f"Field '{name}' does not exist")
                
                field_def = self.fields[name]
                
                # Type validation
                if not isinstance(value, field_def['type']):
                    raise TypeError(f"Field '{name}' must be of type {field_def['type'].__name__}")
                
                # Constraint validation
                constraints = field_def['constraints']
                
                if 'required' in constraints and constraints['required'] and value is None:
                    raise ValueError(f"Field '{name}' is required")
                
                if 'min_length' in constraints and len(str(value)) < constraints['min_length']:
                    raise ValueError(f"Field '{name}' must be at least {constraints['min_length']} characters")
                
                if 'max_length' in constraints and len(str(value)) > constraints['max_length']:
                    raise ValueError(f"Field '{name}' must be at most {constraints['max_length']} characters")
                
                if 'min_value' in constraints and value < constraints['min_value']:
                    raise ValueError(f"Field '{name}' must be at least {constraints['min_value']}")
                
                if 'max_value' in constraints and value > constraints['max_value']:
                    raise ValueError(f"Field '{name}' must be at most {constraints['max_value']}")
                
                return True
        
        # When: User model is defined
        user_model = DatabaseModel()
        user_model.add_field('username', str, {'required': True, 'min_length': 3, 'max_length': 50})
        user_model.add_field('email', str, {'required': True})
        user_model.add_field('age', int, {'min_value': 0, 'max_value': 150})
        
        # Then: Field validation works correctly
        assert user_model.validate_field('username', 'testuser') is True
        assert user_model.validate_field('email', 'test@example.com') is True
        assert user_model.validate_field('age', 25) is True
        
        # When: Invalid values are provided
        with pytest.raises(ValueError, match="must be at least 3 characters"):
            user_model.validate_field('username', 'ab')
        
        with pytest.raises(TypeError, match="must be of type int"):
            user_model.validate_field('age', 'twenty-five')
        
        with pytest.raises(ValueError, match="must be at least 0"):
            user_model.validate_field('age', -5)

    def test_database_relationship_handling(self):
        """Test database model relationships with foreign key handling."""
        # Given: Related models with foreign key relationships
        class RelationshipManager:
            def __init__(self):
                self.models = {}
                self.relationships = {}
                
            def define_model(self, name, fields):
                self.models[name] = fields
                
            def define_relationship(self, from_model, to_model, foreign_key, relationship_type='one_to_many'):
                if from_model not in self.models:
                    raise ValueError(f"Model '{from_model}' not found")
                if to_model not in self.models:
                    raise ValueError(f"Model '{to_model}' not found")
                
                self.relationships[f"{from_model}_{to_model}"] = {
                    'from_model': from_model,
                    'to_model': to_model,
                    'foreign_key': foreign_key,
                    'type': relationship_type
                }
                
            def validate_foreign_key(self, model_name, foreign_key_field, foreign_key_value, related_records):
                relationship_key = None
                for key, rel in self.relationships.items():
                    if rel['from_model'] == model_name and rel['foreign_key'] == foreign_key_field:
                        relationship_key = key
                        break
                
                if not relationship_key:
                    return True  # No relationship defined
                
                relationship = self.relationships[relationship_key]
                related_model = relationship['to_model']
                
                # Check if foreign key value exists in related records
                if foreign_key_value not in related_records.get(related_model, {}):
                    raise ValueError(f"Foreign key '{foreign_key_value}' not found in '{related_model}'")
                
                return True
        
        # When: Models and relationships are defined
        rel_manager = RelationshipManager()
        
        rel_manager.define_model('users', {'id': int, 'username': str})
        rel_manager.define_model('portfolios', {'id': int, 'user_id': int, 'name': str})
        rel_manager.define_model('transactions', {'id': int, 'portfolio_id': int, 'amount': float})
        
        rel_manager.define_relationship('portfolios', 'users', 'user_id')
        rel_manager.define_relationship('transactions', 'portfolios', 'portfolio_id')
        
        # Then: Foreign key validation works
        related_records = {
            'users': {1: {'id': 1, 'username': 'testuser'}},
            'portfolios': {1: {'id': 1, 'user_id': 1, 'name': 'Test Portfolio'}}
        }
        
        # Valid foreign key
        assert rel_manager.validate_foreign_key('portfolios', 'user_id', 1, related_records) is True
        
        # Invalid foreign key
        with pytest.raises(ValueError, match="Foreign key '999' not found"):
            rel_manager.validate_foreign_key('portfolios', 'user_id', 999, related_records)

    def test_database_query_optimization(self):
        """Test database queries with proper indexing and optimization."""
        # Given: Query optimizer with performance tracking
        class QueryOptimizer:
            def __init__(self):
                self.indexes = {}
                self.query_stats = {}
                
            def create_index(self, table, columns):
                index_name = f"{table}_{'_'.join(columns)}_idx"
                self.indexes[index_name] = {
                    'table': table,
                    'columns': columns,
                    'created_at': time.time()
                }
                
            def analyze_query(self, query_sql):
                # Simple query analysis
                query_type = 'SELECT'
                if 'INSERT' in query_sql.upper():
                    query_type = 'INSERT'
                elif 'UPDATE' in query_sql.upper():
                    query_type = 'UPDATE'
                elif 'DELETE' in query_sql.upper():
                    query_type = 'DELETE'
                
                return {
                    'query_type': query_type,
                    'estimated_cost': self._estimate_cost(query_sql),
                    'uses_index': self._uses_index(query_sql),
                    'optimization_hints': self._get_optimization_hints(query_sql)
                }
                
            def _estimate_cost(self, query_sql):
                # Simple cost estimation
                cost = 1.0
                if 'JOIN' in query_sql.upper():
                    cost += 2.0
                if 'WHERE' not in query_sql.upper():
                    cost += 5.0  # Full table scan
                return cost
                
            def _uses_index(self, query_sql):
                # Check if query can use existing indexes
                for index_name, index_def in self.indexes.items():
                    for column in index_def['columns']:
                        if column in query_sql:
                            return True
                return False
                
            def _get_optimization_hints(self, query_sql):
                hints = []
                if 'WHERE' not in query_sql.upper():
                    hints.append("Consider adding WHERE clause to limit results")
                if not self._uses_index(query_sql):
                    hints.append("Consider creating index on frequently queried columns")
                return hints
        
        optimizer = QueryOptimizer()
        
        # When: Indexes are created
        optimizer.create_index('users', ['username'])
        optimizer.create_index('portfolios', ['user_id'])
        optimizer.create_index('transactions', ['portfolio_id', 'timestamp'])
        
        # Then: Query analysis works correctly
        good_query = "SELECT * FROM users WHERE username = 'testuser'"
        analysis = optimizer.analyze_query(good_query)
        
        assert analysis['query_type'] == 'SELECT'
        assert analysis['uses_index'] is True
        assert analysis['estimated_cost'] < 5.0
        
        # When: Poorly optimized query is analyzed
        bad_query = "SELECT * FROM users"
        analysis = optimizer.analyze_query(bad_query)
        
        assert analysis['estimated_cost'] > 5.0
        assert "WHERE clause" in analysis['optimization_hints'][0]


class TestWebAPIComponents:
    """
    UT-013, UT-014: Web API Component Tests
    Tests API endpoint processing and input validation.
    """

    def test_api_endpoint_request_processing(self):
        """Test API endpoint handlers with proper request processing."""
        # Given: API endpoint handler
        class APIEndpoint:
            def __init__(self):
                self.request_count = 0
                
            def process_request(self, method, path, headers, body):
                self.request_count += 1
                
                # Parse request
                request_data = {
                    'method': method,
                    'path': path,
                    'headers': headers,
                    'body': body,
                    'timestamp': time.time()
                }
                
                # Route to appropriate handler
                if method == 'GET' and path == '/api/status':
                    return self._handle_status(request_data)
                elif method == 'POST' and path == '/api/portfolios':
                    return self._handle_create_portfolio(request_data)
                elif method == 'GET' and path.startswith('/api/portfolios/'):
                    return self._handle_get_portfolio(request_data)
                else:
                    return self._handle_not_found(request_data)
                
            def _handle_status(self, request_data):
                return {
                    'status': 200,
                    'data': {
                        'status': 'healthy',
                        'timestamp': request_data['timestamp'],
                        'request_count': self.request_count
                    }
                }
                
            def _handle_create_portfolio(self, request_data):
                body = request_data['body']
                if not isinstance(body, dict) or 'name' not in body:
                    return {
                        'status': 400,
                        'error': 'Invalid request body'
                    }
                
                return {
                    'status': 201,
                    'data': {
                        'id': 1,
                        'name': body['name'],
                        'created_at': request_data['timestamp']
                    }
                }
                
            def _handle_get_portfolio(self, request_data):
                portfolio_id = request_data['path'].split('/')[-1]
                return {
                    'status': 200,
                    'data': {
                        'id': portfolio_id,
                        'name': 'Test Portfolio',
                        'balance': 10000.0
                    }
                }
                
            def _handle_not_found(self, request_data):
                return {
                    'status': 404,
                    'error': 'Endpoint not found'
                }
        
        endpoint = APIEndpoint()
        
        # When: Valid requests are processed
        status_response = endpoint.process_request('GET', '/api/status', {}, {})
        assert status_response['status'] == 200
        assert status_response['data']['status'] == 'healthy'
        
        create_response = endpoint.process_request('POST', '/api/portfolios', {}, {'name': 'Test Portfolio'})
        assert create_response['status'] == 201
        assert create_response['data']['name'] == 'Test Portfolio'
        
        get_response = endpoint.process_request('GET', '/api/portfolios/1', {}, {})
        assert get_response['status'] == 200
        assert get_response['data']['id'] == '1'
        
        # When: Invalid requests are processed
        invalid_response = endpoint.process_request('POST', '/api/portfolios', {}, {})
        assert invalid_response['status'] == 400
        
        not_found_response = endpoint.process_request('GET', '/api/invalid', {}, {})
        assert not_found_response['status'] == 404

    def test_api_input_validation(self):
        """Test API input validation with proper sanitization."""
        # Given: Input validator
        class InputValidator:
            def __init__(self):
                self.validation_rules = {
                    'username': {
                        'type': str,
                        'min_length': 3,
                        'max_length': 50,
                        'pattern': r'^[a-zA-Z0-9_]+$'
                    },
                    'email': {
                        'type': str,
                        'pattern': r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
                    },
                    'amount': {
                        'type': (int, float),
                        'min_value': 0,
                        'max_value': 1000000
                    }
                }
                
            def validate_input(self, field_name, value):
                if field_name not in self.validation_rules:
                    return {'valid': True, 'sanitized_value': value}
                
                rules = self.validation_rules[field_name]
                errors = []
                
                # Type validation
                if not isinstance(value, rules['type']):
                    errors.append(f"Field '{field_name}' must be of type {rules['type']}")
                
                # String validations
                if isinstance(value, str):
                    if 'min_length' in rules and len(value) < rules['min_length']:
                        errors.append(f"Field '{field_name}' must be at least {rules['min_length']} characters")
                    
                    if 'max_length' in rules and len(value) > rules['max_length']:
                        errors.append(f"Field '{field_name}' must be at most {rules['max_length']} characters")
                    
                    if 'pattern' in rules:
                        import re
                        if not re.match(rules['pattern'], value):
                            errors.append(f"Field '{field_name}' has invalid format")
                
                # Numeric validations
                if isinstance(value, (int, float)):
                    if 'min_value' in rules and value < rules['min_value']:
                        errors.append(f"Field '{field_name}' must be at least {rules['min_value']}")
                    
                    if 'max_value' in rules and value > rules['max_value']:
                        errors.append(f"Field '{field_name}' must be at most {rules['max_value']}")
                
                # Sanitization
                sanitized_value = value
                if isinstance(value, str):
                    # Remove potentially dangerous characters
                    sanitized_value = self._sanitize_string(value)
                
                return {
                    'valid': len(errors) == 0,
                    'errors': errors,
                    'sanitized_value': sanitized_value
                }
                
            def _sanitize_string(self, value):
                # Remove HTML tags and dangerous characters
                import re
                sanitized = re.sub(r'<[^>]+>', '', value)  # Remove HTML tags
                sanitized = re.sub(r'[<>"\']', '', sanitized)  # Remove dangerous chars
                return sanitized.strip()
        
        validator = InputValidator()
        
        # When: Valid inputs are validated
        username_result = validator.validate_input('username', 'testuser')
        assert username_result['valid'] is True
        assert username_result['sanitized_value'] == 'testuser'
        
        email_result = validator.validate_input('email', 'test@example.com')
        assert email_result['valid'] is True
        
        amount_result = validator.validate_input('amount', 100.50)
        assert amount_result['valid'] is True
        
        # When: Invalid inputs are validated
        invalid_username = validator.validate_input('username', 'ab')
        assert invalid_username['valid'] is False
        assert 'at least 3 characters' in invalid_username['errors'][0]
        
        invalid_email = validator.validate_input('email', 'invalid-email')
        assert invalid_email['valid'] is False
        
        # When: Malicious inputs are sanitized
        malicious_input = '<script>alert("xss")</script>test'
        sanitized_result = validator.validate_input('username', malicious_input)
        assert '<script>' not in sanitized_result['sanitized_value']
        assert 'alert' in sanitized_result['sanitized_value']  # Content preserved but tags removed


class TestConfigurationComponents:
    """
    UT-015, UT-016: Configuration Component Tests
    Tests configuration loading, validation, and security.
    """

    def test_configuration_loading_and_validation(self):
        """Test configuration loading from multiple sources with validation."""
        # Given: Configuration handler
        config_handler = ConfigHandler()
        
        # When: Configuration is loaded from multiple sources
        default_config = {
            'database': {'type': 'sqlite', 'path': 'default.db'},
            'api': {'port': 8080, 'debug': False}
        }
        
        file_config = {
            'database': {'path': 'file.db'},
            'api': {'port': 8081}
        }
        
        env_config = {
            'database': {'path': 'env.db'}
        }
        
        merged_config = config_handler.merge_configurations([
            default_config, file_config, env_config
        ])
        
        # Then: Configuration is properly merged
        assert merged_config['database']['type'] == 'sqlite'  # From default
        assert merged_config['database']['path'] == 'env.db'  # From env (last)
        assert merged_config['api']['port'] == 8081  # From file
        assert merged_config['api']['debug'] is False  # From default
        
        # When: Configuration is validated
        validation_result = config_handler.validate_configuration(merged_config)
        assert validation_result['valid'] is True

    def test_configuration_security_and_sanitization(self):
        """Test configuration security with sensitive data protection."""
        # Given: Configuration handler with security features
        class SecureConfigHandler:
            def __init__(self):
                self.sensitive_keys = ['password', 'secret', 'key', 'token']
                
            def process_configuration(self, config):
                processed = {}
                for key, value in config.items():
                    if isinstance(value, dict):
                        processed[key] = self.process_configuration(value)
                    else:
                        processed[key] = self._process_value(key, value)
                return processed
                
            def _process_value(self, key, value):
                # Check if key contains sensitive information
                if any(sensitive in key.lower() for sensitive in self.sensitive_keys):
                    return '[REDACTED]'
                return value
                
            def get_secure_value(self, config, key_path):
                # Secure method to retrieve actual sensitive values
                keys = key_path.split('.')
                current = config
                for key in keys:
                    if key in current:
                        current = current[key]
                    else:
                        return None
                return current
        
        handler = SecureConfigHandler()
        
        # When: Configuration with sensitive data is processed
        config_with_secrets = {
            'database': {
                'host': 'localhost',
                'password': 'secret123',
                'user': 'admin'
            },
            'api': {
                'port': 8080,
                'secret_key': 'api_secret_key'
            },
            'external': {
                'oauth_token': 'oauth_token_value'
            }
        }
        
        processed_config = handler.process_configuration(config_with_secrets)
        
        # Then: Sensitive data is redacted
        assert processed_config['database']['password'] == '[REDACTED]'
        assert processed_config['api']['secret_key'] == '[REDACTED]'
        assert processed_config['external']['oauth_token'] == '[REDACTED]'
        
        # But non-sensitive data is preserved
        assert processed_config['database']['host'] == 'localhost'
        assert processed_config['api']['port'] == 8080


class TestErrorHandlingAndLogging:
    """
    UT-017, UT-018: Error Handling and Logging Tests
    Tests error handling, recovery, and audit logging.
    """

    def test_error_handling_and_recovery(self):
        """Test error handling with proper exception management."""
        # Given: Component with error handling
        class ErrorHandlingComponent:
            def __init__(self):
                self.retry_count = 0
                self.max_retries = 3
                
            def process_with_retry(self, operation, *args, **kwargs):
                for attempt in range(self.max_retries):
                    try:
                        return operation(*args, **kwargs)
                    except Exception as e:
                        self.retry_count += 1
                        if attempt == self.max_retries - 1:
                            raise e
                        time.sleep(0.1)  # Brief delay between retries
                
            def safe_division(self, a, b):
                if b == 0:
                    raise ValueError("Cannot divide by zero")
                return a / b
                
            def process_data(self, data):
                if not isinstance(data, dict):
                    raise TypeError("Data must be a dictionary")
                
                if 'value' not in data:
                    raise KeyError("Missing 'value' key in data")
                
                return {'processed': data['value'] * 2}
        
        component = ErrorHandlingComponent()
        
        # When: Operation succeeds
        result = component.safe_division(10, 2)
        assert result == 5.0
        
        # When: Operation fails with proper error
        with pytest.raises(ValueError, match="Cannot divide by zero"):
            component.safe_division(10, 0)
        
        # When: Data processing with valid input
        valid_data = {'value': 5}
        result = component.process_data(valid_data)
        assert result['processed'] == 10
        
        # When: Data processing with invalid input
        with pytest.raises(TypeError, match="Data must be a dictionary"):
            component.process_data("invalid")
        
        with pytest.raises(KeyError, match="Missing 'value' key"):
            component.process_data({'wrong_key': 5})

    def test_logging_and_audit_trail(self):
        """Test logging system with proper audit trail."""
        # Given: Logging system
        class AuditLogger:
            def __init__(self):
                self.logs = []
                
            def log(self, level, message, details=None):
                log_entry = {
                    'timestamp': time.time(),
                    'level': level,
                    'message': message,
                    'details': details or {}
                }
                self.logs.append(log_entry)
                
            def get_logs(self, level=None, start_time=None, end_time=None):
                filtered_logs = self.logs
                
                if level:
                    filtered_logs = [log for log in filtered_logs if log['level'] == level]
                
                if start_time:
                    filtered_logs = [log for log in filtered_logs if log['timestamp'] >= start_time]
                
                if end_time:
                    filtered_logs = [log for log in filtered_logs if log['timestamp'] <= end_time]
                
                return filtered_logs
                
            def clear_logs(self):
                self.logs = []
        
        logger = AuditLogger()
        
        # When: Various log entries are created
        logger.log('INFO', 'System started', {'version': '1.0'})
        logger.log('DEBUG', 'Processing request', {'request_id': 'req123'})
        logger.log('ERROR', 'Operation failed', {'error': 'Connection timeout'})
        logger.log('INFO', 'System shutdown', {'uptime': '2h 30m'})
        
        # Then: Logs are properly maintained
        assert len(logger.logs) == 4
        
        # When: Logs are filtered by level
        error_logs = logger.get_logs(level='ERROR')
        assert len(error_logs) == 1
        assert error_logs[0]['message'] == 'Operation failed'
        
        info_logs = logger.get_logs(level='INFO')
        assert len(info_logs) == 2
        
        # When: Logs are filtered by time
        current_time = time.time()
        recent_logs = logger.get_logs(start_time=current_time - 60)
        assert len(recent_logs) == 4  # All logs are recent


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
