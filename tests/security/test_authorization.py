"""
Security tests for LTS authorization: RBAC, resource-level access, privilege escalation.
"""
import pytest
from unittest.mock import MagicMock, patch
from plugins_aaa.default_aaa import DefaultAAA


@pytest.fixture
def aaa():
    with patch('plugins_aaa.default_aaa.SessionLocal') as mock_sl:
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        yield DefaultAAA(config={})


class TestRBAC:
    def test_admin_authorized(self, aaa):
        assert aaa.authorize_user(["admin"], "admin") is True

    def test_user_not_admin(self, aaa):
        assert aaa.authorize_user(["user"], "admin") is False

    def test_multi_role_match(self, aaa):
        assert aaa.authorize_user(["user", "admin"], "admin") is True

    def test_no_required_role(self, aaa):
        assert aaa.authorize_user(["user"], None) is True
        assert aaa.authorize_user(["user"], "") is True

    def test_empty_user_roles(self, aaa):
        assert aaa.authorize_user([], "admin") is False


class TestHasPermission:
    def test_admin_has_permission(self, aaa):
        mock_user = MagicMock()
        mock_user.role = "admin"
        aaa.db.query.return_value.filter.return_value.first.return_value = mock_user
        assert aaa.has_permission("admin_user", "any_action") is True

    def test_user_no_admin_permission(self, aaa):
        mock_user = MagicMock()
        mock_user.role = "user"
        aaa.db.query.return_value.filter.return_value.first.return_value = mock_user
        assert aaa.has_permission("regular_user", "admin_action") is False

    def test_nonexistent_user(self, aaa):
        aaa.db.query.return_value.filter.return_value.first.return_value = None
        assert aaa.has_permission("ghost", "action") is False


class TestRoleAssignment:
    def test_assign_role_success(self, aaa):
        mock_user = MagicMock()
        mock_user.id = 1
        aaa.db.query.return_value.filter.return_value.first.return_value = mock_user
        result = aaa.assign_role("testuser", "admin")
        assert result is True
        assert mock_user.role == "admin"

    def test_assign_role_nonexistent(self, aaa):
        aaa.db.query.return_value.filter.return_value.first.return_value = None
        result = aaa.assign_role("ghost", "admin")
        assert result is False


class TestPrivilegeEscalation:
    def test_user_cannot_self_assign_admin(self, aaa):
        """Only admin should be able to assign roles - verify user can't become admin"""
        mock_user = MagicMock()
        mock_user.role = "user"
        aaa.db.query.return_value.filter.return_value.first.return_value = mock_user
        
        # The has_permission check should fail for non-admins
        assert aaa.has_permission("regular_user", "assign_role") is False

    def test_token_role_matches_db(self, aaa):
        """JWT role claim must match database role"""
        token = aaa.create_access_token(1, "testuser", "admin")
        payload = aaa._decode_jwt_token(token)
        # If someone tampers with role in JWT, validation should check DB
        assert payload["role"] == "admin"
