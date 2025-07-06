"""
test_unit_aaa.py

Unit tests for the AAA (Authentication, Authorization, Accounting) plugin.

This file contains tests for:
- AAA plugin authorization methods (U-006)
- AAA plugin accounting methods (U-007)
"""

import pytest
from unittest.mock import MagicMock, patch
import json

from plugins_aaa.default_aaa import DefaultAAA

@pytest.fixture
def aaa_plugin():
    """
    Provides a DefaultAAA plugin instance with a mocked database session.
    """
    with patch('plugins_aaa.default_aaa.SessionLocal') as mock_session_local:
        mock_db_instance = MagicMock()
        mock_session_local.return_value = mock_db_instance
        
        plugin = DefaultAAA(config={})
        # The plugin's __init__ calls SessionLocal, so its db is already the mock
        yield plugin

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
    
    # Verify that the database's add and commit methods were called
    aaa_plugin.db.add.assert_called_once()
    aaa_plugin.db.commit.assert_called_once()
    
    # Verify the contents of the call to add
    added_object = aaa_plugin.db.add.call_args[0][0]
    assert added_object.user_id == user_id
    assert added_object.action == action
    assert added_object.details == json.dumps(details)
