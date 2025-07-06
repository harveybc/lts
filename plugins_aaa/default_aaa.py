"""
Default AAA (Authentication, Authorization, Accounting) plugin for LTS.
Implements user authentication, role management, and audit logging.
"""
from app.plugin_base import AAAPluginBase
from app.database import SessionLocal, User, Session, AuditLog
from datetime import datetime, timezone, timedelta
import hashlib
import secrets
import json

class AAAPlugin(AAAPluginBase):
    """Default AAA plugin implementation"""
    
    # Plugin-specific parameters
    plugin_params = {
        "session_timeout_hours": 24,
        "password_min_length": 6,
        "max_login_attempts": 3,
        "audit_enabled": True,
        "default_role": "user",
        "admin_role": "admin",
        "hash_algorithm": "sha256",
        "token_length": 32
    }
    
    # Debug variables
    plugin_debug_vars = ["session_timeout_hours", "password_min_length", "max_login_attempts", "audit_enabled"]

    def __init__(self, config=None):
        """Initialize AAA plugin with configuration"""
        super().__init__(config)
        self.db = SessionLocal()

    def set_params(self, **kwargs):
        """Update parameters with global configuration"""
        super().set_params(**kwargs)

    def get_debug_info(self):
        """Return debug information"""
        return super().get_debug_info()

    def add_debug_info(self, debug_info):
        """Add debug information to dictionary"""
        super().add_debug_info(debug_info)

    def register(self, username: str, email: str, password: str, role: str = None) -> bool:
        """Register a new user"""
        try:
            # Check if user already exists
            existing_user = self.db.query(User).filter(
                (User.username == username) | (User.email == email)
            ).first()
            
            if existing_user:
                return False
            
            # Validate password
            if len(password) < self.params["password_min_length"]:
                return False
            
            # Hash password
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            
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
            print(f"Registration error: {e}")
            return False

    def login(self, username: str, password: str) -> dict:
        """Authenticate user and create session"""
        try:
            # Find user
            user = self.db.query(User).filter(User.username == username).first()
            
            if not user or not user.is_active:
                return {"success": False, "message": "Invalid credentials"}
            
            # Check password
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            if user.password_hash != password_hash:
                return {"success": False, "message": "Invalid credentials"}
            
            # Create session
            session_token = self.create_session(user.id)
            
            # Audit log
            if self.params["audit_enabled"]:
                self.audit(user.id, "user_login", f"User {username} logged in")
            
            return {
                "success": True,
                "user_id": user.id,
                "username": user.username,
                "role": user.role,
                "session_token": session_token
            }
            
        except Exception as e:
            print(f"Login error: {e}")
            return {"success": False, "message": "Login failed"}

    def assign_role(self, username: str, role: str) -> bool:
        """Assign role to user"""
        try:
            user = self.db.query(User).filter(User.username == username).first()
            if not user:
                return False
            
            user.role = role
            self.db.commit()
            
            # Audit log
            if self.params["audit_enabled"]:
                self.audit(user.id, "role_assigned", f"Role {role} assigned to {username}")
            
            return True
            
        except Exception as e:
            self.db.rollback()
            print(f"Role assignment error: {e}")
            return False

    def audit(self, user_id: int, action: str, details: str = None) -> None:
        """Log audit event"""
        try:
            if self.params["audit_enabled"]:
                audit_log = AuditLog(
                    user_id=user_id,
                    action=action,
                    details=details,
                    timestamp=datetime.now(timezone.utc)
                )
                self.db.add(audit_log)
                self.db.commit()
        except Exception as e:
            print(f"Audit logging error: {e}")

    def create_session(self, user_id: int) -> str:
        """Create session token for user"""
        try:
            # Generate token
            token = secrets.token_urlsafe(self.params["token_length"])
            
            # Create session
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
            print(f"Session creation error: {e}")
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
            print(f"Session validation error: {e}")
            return {"valid": False, "message": "Validation failed"}
