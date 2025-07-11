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
import json
from unittest.mock import Mock, patch, MagicMock
from sqlalchemy import text
from sqlalchemy.exc import ResourceClosedError
from fastapi.testclient import TestClient

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from app.database import Database, Base
from app.main import main
from plugins_core.default_core import create_app, get_current_user, core_plugin_instance

class TestPluginSystemIntegration:
    """
    INT-001, INT-002, INT-003: Plugin System Integration Tests
    Tests plugin lifecycle, communication, and configuration integration.
    """

    @patch('app.main.load_plugin')
    @patch('app.main.parse_args')
    def test_plugin_lifecycle_management(self, mock_parse_args, mock_load_plugin):
        """INT-001: Test plugins are loaded, initialized, configured, and shut down consistently."""
        mock_parse_args.return_value = (MagicMock(load_config=None), [])
        mock_plugin_instance = MagicMock()
        mock_plugin_instance.plugin_params = {}
        # The CorePlugin now returns a FastAPI app, so we mock that behavior
        mock_core_plugin = MagicMock()
        mock_core_plugin.plugin_params = {}
        
        # Side effect to return the core plugin mock first, then other mocks
        mock_load_plugin.side_effect = [
            (MagicMock(return_value=mock_core_plugin), None),
            (MagicMock(return_value=mock_plugin_instance), None),
            (MagicMock(return_value=mock_plugin_instance), None),
            (MagicMock(return_value=mock_plugin_instance), None),
            (MagicMock(return_value=mock_plugin_instance), None),
            (MagicMock(return_value=mock_plugin_instance), None),
        ]

        with patch('app.main.sys.exit'):
             with patch('plugins_core.default_core.CorePlugin.initialize'):
                main()

        # Check that load_plugin was called for each plugin type
        assert mock_load_plugin.call_count == 6
        # Check that plugins are configured
        mock_plugin_instance.set_params.assert_called()

class TestDatabaseIntegration:
    """
    INT-004, INT-005, INT-006: Database Integration Tests
    """
    @pytest.fixture(scope="function")
    async def db_instance(self):
        db = Database(db_path=":memory:")
        await db.initialize()
        # Create a simple test table for these tests
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY, data TEXT)"))
        yield db
        await db.cleanup()

    @pytest.mark.asyncio
    async def test_database_connection_management(self, db_instance):
        """INT-004: Test database connection pooling and management."""
        async with db_instance.get_session() as session:
            assert session.is_active
            connection = await session.connection()
            assert not connection.closed
        
        assert connection.closed

    @pytest.mark.asyncio
    async def test_transaction_integrity_acid(self, db_instance):
        """INT-005: Test that database transactions maintain ACID properties."""
        try:
            async with db_instance.get_session() as session:
                await session.execute(text("INSERT INTO test_table (data) VALUES (:data)"), {"data": "test1"})
                raise ValueError("Forced error")
        except ValueError:
            pass  # Expected

        async with db_instance.get_session() as session:
            result = await session.execute(text("SELECT * FROM test_table"))
            results = result.fetchall()
            assert len(results) == 0  # Transaction should have been rolled back

        async with db_instance.get_session() as session:
            await session.execute(text("INSERT INTO test_table (data) VALUES (:data)"), {"data": "test2"})
        
        async with db_instance.get_session() as session:
            result = await session.execute(text("SELECT * FROM test_table"))
            results = result.fetchall()
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_orm_and_data_mapping(self, db_instance):
        """INT-006: Test the integration between the data access layer and the database schema."""
        async with db_instance.get_session() as session:
            await session.execute(text("INSERT INTO test_table (data) VALUES (:data)"), {"data": "orm_test"})

        async with db_instance.get_session() as session:
            result = await session.execute(text("SELECT * FROM test_table WHERE data = :data"), {"data": "orm_test"})
            rows = result.fetchall()
            assert len(rows) == 1
            assert rows[0][1] == 'orm_test' # data is the second column

    @pytest.mark.asyncio
    async def test_orm_model_integration(self, db_instance):
        """Test that ORM models can be created and queried."""
        from app.database import User
        async with db_instance.get_session() as session:
            new_user = User(username='testuser', email='test@test.com', password_hash='123')
            session.add(new_user)
        
        async with db_instance.get_session() as session:
            result = await session.execute(text("SELECT * FROM users WHERE username = :username"), {"username": "testuser"})
            user = result.fetchone()
            assert user is not None
            assert user.email == 'test@test.com'

class TestWebAPIIntegration:
    """
    INT-007, INT-008, INT-009: Web API Integration Tests
    """
    @pytest.fixture
    def client(self):
        app = create_app()
        # Mock the AAA plugin for the core plugin instance
        core_plugin_instance.initialize(plugins={'aaa': MagicMock()})
        with TestClient(app) as c:
            yield c

    def test_web_api_authentication_integration(self, client):
        """INT-007: Test that API endpoints are correctly protected by the authentication plugin."""
        # Hit the secure endpoint without a token
        response = client.get("/api/v1/secure")
        assert response.status_code == 403 # Now expecting 403 based on our dependency

        # Hit the secure endpoint with a valid token
        client.app.dependency_overrides[get_current_user] = lambda: {"username": "testuser"}
        response = client.get("/api/v1/secure", headers={"Authorization": "Bearer validtoken"})
        assert response.status_code == 200
        
        # Clear overrides
        client.app.dependency_overrides = {}

class TestConfigurationIntegration:
    """
    INT-010, INT-011: Configuration Integration Tests
    """
    def test_multi_source_configuration_merging(self):
        """INT-010: Test that configuration from different sources is merged with the correct precedence."""
        from app.config_merger import merge_config
        default_config = {"a": 1, "b": 1}
        file_config_data = {"b": 2, "c": 2}
        
        merged = merge_config(default_config, {}, {}, file_config_data, {}, {})
        
        assert merged['a'] == 1
        assert merged['b'] == 2
        assert merged['c'] == 2

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
