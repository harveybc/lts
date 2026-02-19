"""
Default AAA (Authentication, Authorization, Accounting) plugin for LTS.
Implements user authentication, role management, and audit logging.
Production-grade security: bcrypt password hashing, JWT tokens, account lockout,
Google OAuth 2.0 support, and comprehensive audit trail.
"""
import os as _os
_QUIET = _os.environ.get('LTS_QUIET', '0') == '1'

from app.plugin_base import AAAPluginBase
from app.database import SyncSessionLocal as SessionLocal, User, Session, AuditLog
from datetime import datetime, timezone, timedelta
import bcrypt
import secrets
import json
import hashlib

# JWT support
from jose import jwt, JWTError

class DefaultAAA(AAAPluginBase):
    """Default AAA plugin implementation with production-grade security"""
    
    # Plugin-specific parameters
    plugin_params = {
        "session_timeout_hours": 24,
        "password_min_length": 8,
        "max_login_attempts": 5,
        "lockout_duration_minutes": 30,
        "audit_enabled": True,
        "default_role": "user",
        "admin_role": "admin",
        "token_length": 32,
        "jwt_secret": _os.environ.get("LTS_JWT_SECRET", secrets.token_hex(32)),
        "jwt_algorithm": "HS256",
        "jwt_access_expire_minutes": 30,
        "jwt_refresh_expire_days": 7,
        "password_require_uppercase": True,
        "password_require_digit": True,
        "google_client_id": _os.environ.get("LTS_GOOGLE_CLIENT_ID", ""),
        "google_client_secret": _os.environ.get("LTS_GOOGLE_CLIENT_SECRET", ""),
    }
    
    # Debug variables
    plugin_debug_vars = ["session_timeout_hours", "password_min_length", "max_login_attempts", "audit_enabled"]

    def __init__(self, config=None):
        """Initialize AAA plugin with configuration"""
        super().__init__(config)
        self.db = SessionLocal()
        # Track failed login attempts: {username: [(timestamp, ip), ...]}
        self._failed_attempts = {}

    def set_params(self, **kwargs):
        """Update parameters with global configuration"""
        super().set_params(**kwargs)

    def get_debug_info(self):
        """Return debug information"""
        return super().get_debug_info()

    def add_debug_info(self, debug_info):
        """Add debug information to dictionary"""
        super().add_debug_info(debug_info)

    # --- Password Hashing ---

    def _hash_password(self, password: str) -> str:
        """Hash password using bcrypt"""
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password against bcrypt hash"""
        try:
            return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
        except Exception:
            return False

    def _validate_password_complexity(self, password: str) -> tuple:
        """Validate password meets complexity requirements. Returns (ok, message)."""
        if len(password) < self.params["password_min_length"]:
            return False, f"Password must be at least {self.params['password_min_length']} characters"
        if self.params.get("password_require_uppercase") and not any(c.isupper() for c in password):
            return False, "Password must contain at least one uppercase letter"
        if self.params.get("password_require_digit") and not any(c.isdigit() for c in password):
            return False, "Password must contain at least one digit"
        return True, "OK"

    # --- JWT Token Management ---

    def _create_jwt_token(self, data: dict, expires_delta: timedelta = None, token_type: str = "access") -> str:
        """Create a JWT token"""
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        elif token_type == "refresh":
            expire = datetime.now(timezone.utc) + timedelta(days=self.params["jwt_refresh_expire_days"])
        else:
            expire = datetime.now(timezone.utc) + timedelta(minutes=self.params["jwt_access_expire_minutes"])
        to_encode.update({"exp": expire, "type": token_type, "iat": datetime.now(timezone.utc)})
        return jwt.encode(to_encode, self.params["jwt_secret"], algorithm=self.params["jwt_algorithm"])

    def _decode_jwt_token(self, token: str) -> dict:
        """Decode and validate a JWT token. Returns payload or None."""
        try:
            payload = jwt.decode(token, self.params["jwt_secret"], algorithms=[self.params["jwt_algorithm"]])
            return payload
        except JWTError:
            return None

    def create_access_token(self, user_id: int, username: str, role: str) -> str:
        """Create a short-lived JWT access token"""
        return self._create_jwt_token(
            {"sub": username, "user_id": user_id, "role": role},
            token_type="access"
        )

    def create_refresh_token(self, user_id: int, username: str) -> str:
        """Create a long-lived JWT refresh token"""
        return self._create_jwt_token(
            {"sub": username, "user_id": user_id},
            token_type="refresh"
        )

    def refresh_access_token(self, refresh_token: str) -> dict:
        """Use refresh token to get a new access token"""
        payload = self._decode_jwt_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            return {"success": False, "message": "Invalid refresh token"}
        
        user = self.db.query(User).filter(User.username == payload["sub"]).first()
        if not user or not user.is_active:
            return {"success": False, "message": "User not found or inactive"}
        
        access_token = self.create_access_token(user.id, user.username, user.role)
        return {"success": True, "access_token": access_token}

    def validate_jwt(self, token: str) -> dict:
        """Validate a JWT token and return user info"""
        payload = self._decode_jwt_token(token)
        if not payload:
            return {"valid": False, "message": "Invalid or expired token"}
        
        return {
            "valid": True,
            "user_id": payload.get("user_id"),
            "username": payload.get("sub"),
            "role": payload.get("role"),
            "token_type": payload.get("type")
        }

    # --- Account Lockout ---

    def _is_locked_out(self, username: str) -> bool:
        """Check if account is locked due to failed attempts"""
        attempts = self._failed_attempts.get(username, [])
        if not attempts:
            return False
        
        # Clean old attempts
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.params["lockout_duration_minutes"])
        recent = [a for a in attempts if a[0] > cutoff]
        self._failed_attempts[username] = recent
        
        return len(recent) >= self.params["max_login_attempts"]

    def _record_failed_attempt(self, username: str, ip: str = None):
        """Record a failed login attempt"""
        if username not in self._failed_attempts:
            self._failed_attempts[username] = []
        self._failed_attempts[username].append((datetime.now(timezone.utc), ip))

    def _clear_failed_attempts(self, username: str):
        """Clear failed login attempts on successful login"""
        self._failed_attempts.pop(username, None)

    # --- Core AAA Methods ---

    def register(self, username: str, email: str, password: str, role: str = None) -> bool:
        """Register a new user with bcrypt password hashing"""
        try:
            # Check if user already exists
            existing_user = self.db.query(User).filter(
                (User.username == username) | (User.email == email)
            ).first()
            
            if existing_user:
                return False
            
            # Validate password complexity
            ok, msg = self._validate_password_complexity(password)
            if not ok:
                return False
            
            # Hash password with bcrypt
            password_hash = self._hash_password(password)
            
            # Create user
            user = User(
                username=username,
                email=email,
                password_hash=password_hash,
                role=role or self.params["default_role"],
                is_active=True,
                created_at=datetime.now(timezone.utc)
            )
            
            self.db.add(user)
            self.db.commit()
            
            # Audit log
            if self.params["audit_enabled"]:
                self.audit(user.id, "user_registered", f"User {username} registered")
            
            return True
            
        except Exception as e:
            self.db.rollback()
            if not _QUIET: print(f"Registration error: {e}")
            return False

    def authorize_user(self, user_roles: list, required_role: str) -> bool:
        """Authorize user based on roles."""
        if not required_role:
            return True
        return any(role == required_role for role in user_roles)

    def audit_action(self, user_id: int, action: str, details: dict = None):
        """Log an audit trail for a user action."""
        if not self.params.get("audit_enabled"):
            return

        try:
            audit_log_entry = AuditLog(
                user_id=user_id,
                action=action,
                details=json.dumps(details) if details else "{}",
                timestamp=datetime.now(timezone.utc)
            )
            self.db.add(audit_log_entry)
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            if not _QUIET: print(f"Audit log error: {e}")

    def audit(self, user_id: int, action: str, details: str = None):
        """Log audit entry (string details version)."""
        if not self.params.get("audit_enabled"):
            return
        try:
            audit_log_entry = AuditLog(
                user_id=user_id,
                action=action,
                details=details or "",
                timestamp=datetime.now(timezone.utc)
            )
            self.db.add(audit_log_entry)
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            if not _QUIET: print(f"Audit log error: {e}")

    def login(self, username: str, password: str, ip: str = None) -> dict:
        """Authenticate user with bcrypt and return JWT tokens"""
        try:
            # Check lockout
            if self._is_locked_out(username):
                return {"success": False, "message": "Account temporarily locked due to too many failed attempts"}
            
            # Find user
            user = self.db.query(User).filter(User.username == username).first()
            
            if not user or not user.is_active:
                self._record_failed_attempt(username, ip)
                return {"success": False, "message": "Invalid credentials"}
            
            # Verify password with bcrypt
            if not self._verify_password(password, user.password_hash):
                self._record_failed_attempt(username, ip)
                if self.params["audit_enabled"]:
                    self.audit(user.id, "login_failed", f"Failed login attempt from {ip}")
                return {"success": False, "message": "Invalid credentials"}
            
            # Clear failed attempts
            self._clear_failed_attempts(username)
            
            # Create JWT tokens
            access_token = self.create_access_token(user.id, user.username, user.role)
            refresh_token = self.create_refresh_token(user.id, user.username)
            
            # Also create a session record
            session_token = secrets.token_hex(self.params["token_length"])
            expires_at = datetime.now(timezone.utc) + timedelta(hours=self.params["session_timeout_hours"])
            
            session = Session(
                user_id=user.id,
                token=session_token,
                expires_at=expires_at,
                created_at=datetime.now(timezone.utc)
            )
            
            self.db.add(session)
            self.db.commit()
            
            # Audit log
            if self.params["audit_enabled"]:
                self.audit(user.id, "user_login", f"User {username} logged in from {ip}")
            
            return {
                "success": True,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "session_token": session_token,
                "user_id": user.id,
                "role": user.role
            }
            
        except Exception as e:
            self.db.rollback()
            if not _QUIET: print(f"Login error: {e}")
            return {"success": False, "message": "An error occurred"}

    def assign_role(self, username: str, role: str) -> bool:
        """Assign role to user"""
        try:
            user = self.db.query(User).filter(User.username == username).first()
            if not user:
                return False
            
            user.role = role
            self.db.commit()
            
            if self.params["audit_enabled"]:
                self.audit(user.id, "role_assigned", f"Role {role} assigned to {username}")
            
            return True
            
        except Exception as e:
            self.db.rollback()
            if not _QUIET: print(f"Role assignment error: {e}")
            return False

    def create_session(self, user_id: int) -> str:
        """Create session token for user"""
        try:
            token = secrets.token_urlsafe(self.params["token_length"])
            expires_at = datetime.now(timezone.utc) + timedelta(hours=self.params["session_timeout_hours"])
            session = Session(
                user_id=user_id,
                token=token,
                created_at=datetime.now(timezone.utc),
                expires_at=expires_at
            )
            self.db.add(session)
            self.db.commit()
            return token
        except Exception as e:
            self.db.rollback()
            if not _QUIET: print(f"Session creation error: {e}")
            return None

    def validate_session(self, token: str) -> dict:
        """Validate session token"""
        try:
            session = self.db.query(Session).filter(Session.token == token).first()
            if not session:
                return {"valid": False, "message": "Invalid session"}
            if session.expires_at < datetime.now(timezone.utc):
                return {"valid": False, "message": "Session expired"}
            user = self.db.query(User).filter(User.id == session.user_id).first()
            return {
                "valid": True,
                "user_id": user.id,
                "username": user.username,
                "role": user.role
            }
        except Exception as e:
            if not _QUIET: print(f"Session validation error: {e}")
            return {"valid": False, "message": "Validation failed"}

    def authenticate(self, username, password):
        result = self.login(username, password)
        return result.get("success", False)

    def has_permission(self, username, action):
        user = self.db.query(User).filter(User.username == username).first()
        if user and user.role == self.params.get("admin_role", "admin"):
            return True
        return False

    # --- Google OAuth 2.0 ---

    def google_oauth_login(self, id_token_data: dict) -> dict:
        """
        Process Google OAuth login. Expects decoded Google ID token data with
        'email', 'name', 'sub' (Google user ID).
        Creates user if not exists, returns JWT tokens.
        """
        try:
            email = id_token_data.get("email")
            name = id_token_data.get("name", email.split("@")[0])
            google_id = id_token_data.get("sub")
            
            if not email:
                return {"success": False, "message": "Email not provided in OAuth token"}
            
            # Find or create user
            user = self.db.query(User).filter(User.email == email).first()
            
            if not user:
                # Create new user from OAuth
                user = User(
                    username=name,
                    email=email,
                    password_hash=self._hash_password(secrets.token_hex(32)),  # Random password
                    role=self.params["default_role"],
                    is_active=True,
                    created_at=datetime.now(timezone.utc)
                )
                self.db.add(user)
                self.db.commit()
                
                if self.params["audit_enabled"]:
                    self.audit(user.id, "oauth_register", f"User {name} registered via Google OAuth")
            
            if not user.is_active:
                return {"success": False, "message": "Account is disabled"}
            
            # Create tokens
            access_token = self.create_access_token(user.id, user.username, user.role)
            refresh_token = self.create_refresh_token(user.id, user.username)
            
            if self.params["audit_enabled"]:
                self.audit(user.id, "oauth_login", f"User {user.username} logged in via Google OAuth")
            
            return {
                "success": True,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "user_id": user.id,
                "role": user.role
            }
            
        except Exception as e:
            self.db.rollback()
            if not _QUIET: print(f"OAuth login error: {e}")
            return {"success": False, "message": "OAuth login failed"}
