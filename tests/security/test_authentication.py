"""
Security tests for LTS authentication: bcrypt, JWT, lockout, OAuth, password complexity.
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
import json

from plugins_aaa.default_aaa import DefaultAAA


@pytest.fixture
def aaa():
    with patch('plugins_aaa.default_aaa.SessionLocal') as mock_sl:
        mock_db = MagicMock()
        mock_sl.return_value = mock_db
        plugin = DefaultAAA(config={})
        # For register/login tests, we need to control db behavior
        yield plugin


class TestPasswordHashing:
    def test_bcrypt_hash_not_plaintext(self, aaa):
        h = aaa._hash_password("MyPassword1")
        assert h != "MyPassword1"
        assert h.startswith("$2")  # bcrypt prefix

    def test_bcrypt_verify_correct(self, aaa):
        h = aaa._hash_password("Secret1Pass")
        assert aaa._verify_password("Secret1Pass", h) is True

    def test_bcrypt_verify_wrong(self, aaa):
        h = aaa._hash_password("Secret1Pass")
        assert aaa._verify_password("WrongPass1", h) is False

    def test_bcrypt_different_salts(self, aaa):
        h1 = aaa._hash_password("SamePass1")
        h2 = aaa._hash_password("SamePass1")
        assert h1 != h2  # Different salts

    def test_no_sha256_used(self, aaa):
        """Ensure we're NOT using SHA256 anymore"""
        import hashlib
        h = aaa._hash_password("Test1Pass")
        sha = hashlib.sha256("Test1Pass".encode()).hexdigest()
        assert h != sha


class TestPasswordComplexity:
    def test_too_short(self, aaa):
        ok, msg = aaa._validate_password_complexity("Ab1")
        assert ok is False

    def test_no_uppercase(self, aaa):
        ok, msg = aaa._validate_password_complexity("lowercase123")
        assert ok is False

    def test_no_digit(self, aaa):
        ok, msg = aaa._validate_password_complexity("NoDigitHere")
        assert ok is False

    def test_valid_password(self, aaa):
        ok, msg = aaa._validate_password_complexity("ValidPass1")
        assert ok is True


class TestJWTTokens:
    def test_create_access_token(self, aaa):
        token = aaa.create_access_token(1, "testuser", "user")
        assert token is not None
        payload = aaa._decode_jwt_token(token)
        assert payload["sub"] == "testuser"
        assert payload["type"] == "access"
        assert payload["role"] == "user"

    def test_create_refresh_token(self, aaa):
        token = aaa.create_refresh_token(1, "testuser")
        assert token is not None
        payload = aaa._decode_jwt_token(token)
        assert payload["sub"] == "testuser"
        assert payload["type"] == "refresh"

    def test_token_expiration(self, aaa):
        """Token created with very short expiry should expire"""
        token = aaa._create_jwt_token(
            {"sub": "test", "user_id": 1},
            expires_delta=timedelta(seconds=-1),  # Already expired
            token_type="access"
        )
        payload = aaa._decode_jwt_token(token)
        assert payload is None  # Expired

    def test_token_tampering(self, aaa):
        token = aaa.create_access_token(1, "testuser", "user")
        tampered = token[:-5] + "XXXXX"
        payload = aaa._decode_jwt_token(tampered)
        assert payload is None

    def test_validate_jwt(self, aaa):
        token = aaa.create_access_token(1, "admin", "admin")
        result = aaa.validate_jwt(token)
        assert result["valid"] is True
        assert result["username"] == "admin"
        assert result["role"] == "admin"

    def test_validate_invalid_jwt(self, aaa):
        result = aaa.validate_jwt("invalid.token.here")
        assert result["valid"] is False


class TestAccountLockout:
    def test_lockout_after_max_attempts(self, aaa):
        for i in range(aaa.params["max_login_attempts"]):
            aaa._record_failed_attempt("lockeduser", "1.2.3.4")
        assert aaa._is_locked_out("lockeduser") is True

    def test_no_lockout_under_threshold(self, aaa):
        aaa._record_failed_attempt("user1", "1.2.3.4")
        assert aaa._is_locked_out("user1") is False

    def test_lockout_expires(self, aaa):
        # Manually set old attempts
        old_time = datetime.now(timezone.utc) - timedelta(minutes=60)
        aaa._failed_attempts["olduser"] = [(old_time, "1.2.3.4")] * 10
        assert aaa._is_locked_out("olduser") is False

    def test_clear_on_success(self, aaa):
        for i in range(3):
            aaa._record_failed_attempt("clearuser", "1.2.3.4")
        aaa._clear_failed_attempts("clearuser")
        assert aaa._is_locked_out("clearuser") is False


class TestGoogleOAuth:
    def test_oauth_login_new_user(self, aaa):
        """Test creating a new user via Google OAuth"""
        aaa.db.query.return_value.filter.return_value.first.return_value = None
        
        # Mock the commit and user creation
        mock_user = MagicMock()
        mock_user.id = 99
        mock_user.username = "oauthuser"
        mock_user.role = "user"
        mock_user.is_active = True
        
        # First call (check email) returns None, then after add/commit the user exists
        call_count = [0]
        def side_effect(*args, **kwargs):
            mock_filter = MagicMock()
            if call_count[0] == 0:
                mock_filter.first.return_value = None
                call_count[0] += 1
            else:
                mock_filter.first.return_value = mock_user
            return mock_filter
        
        aaa.db.query.return_value.filter = side_effect
        
        # Patch add to capture the user object
        added_users = []
        def capture_add(obj):
            added_users.append(obj)
            # Make it look like we have the user after commit
            obj.id = 99
        aaa.db.add = capture_add
        aaa.db.commit = MagicMock()
        
        result = aaa.google_oauth_login({
            "email": "oauth@gmail.com",
            "name": "oauthuser",
            "sub": "google-id-123"
        })
        
        assert result["success"] is True
        assert "access_token" in result

    def test_oauth_login_no_email(self, aaa):
        result = aaa.google_oauth_login({"name": "nomail"})
        assert result["success"] is False


class TestSessionManagement:
    def test_validate_session_expired(self, aaa):
        mock_session = MagicMock()
        mock_session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        aaa.db.query.return_value.filter.return_value.first.return_value = mock_session
        result = aaa.validate_session("expired-token")
        assert result["valid"] is False

    def test_validate_session_invalid(self, aaa):
        aaa.db.query.return_value.filter.return_value.first.return_value = None
        result = aaa.validate_session("nonexistent")
        assert result["valid"] is False
