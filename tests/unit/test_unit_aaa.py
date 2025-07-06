"""
test_unit_aaa.py

Unit tests for the AAA (Authentication, Authorization, Accounting) plugin.

This file contains tests for:
- AAA plugin authorization methods (U-006)
- AAA plugin accounting methods (U-007)
"""

import pytest
from unittest.mock import MagicMock, patch

from plugins_aaa.default_aaa import DefaultAAA

@pytest.fixture
def aaa_plugin():
    """
    Provides a DefaultAAA plugin instance with a mocked database.
    """
    with patch('app.database.Database') as mock_db:
        mock_db_instance = MagicMock()
        plugin = DefaultAAA(config={})
        plugin.db = mock_db_instance
        return plugin

def test_aaa_authorization_authorized(aaa_plugin):
    """
    Tests that authorize_user returns True for a user with the required role.
    (U-006: AAA plugin authorization methods)
    """
    user_roles = ['admin', 'user']
    required_role = 'admin'
    assert aaa_plugin.authorize_user(user_roles, required_role) is True

def test_aaa_authorization_unauthorized(aaa_plugin):
    """
    Tests that authorize_user returns False for a user without the required role.
    (U-006: AAA plugin authorization methods)
    """
    user_roles = ['user']
    required_role = 'admin'
    assert aaa_plugin.authorize_user(user_roles, required_role) is False

def test_aaa_accounting_audit_log(aaa_plugin):
    """
    Tests that the audit_action method correctly logs an action to the database.
    (U-007: AAA plugin accounting methods)
    """
    user_id = 1
    action = "test_action"
    details = {"info": "test_details"}
    
    aaa_plugin.audit_action(user_id, action, details)
    
    # Verify that the database's execute method was called with the correct query
    aaa_plugin.db.execute.assert_called_once()
    call_args = aaa_plugin.db.execute.call_args[0][0]
    assert "INSERT INTO audit_log" in str(call_args)
    assert "user_id, action, details" in str(call_args)
