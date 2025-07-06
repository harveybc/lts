"""
AAA plugin: implements register, login, role, audit, session methods securely.
"""
from app.plugin_base import AAABase
import hashlib
import uuid

class AAAPlugin(AAABase):
    plugin_params = {"min_password_length": 8}
    plugin_debug_vars = ["last_action", "last_user"]
    users = {}
    sessions = {}
    audit_log = []

    def register(self, user: str, email: str, password: str, role: str) -> bool:
        if user in self.users or len(password) < self.plugin_params["min_password_length"]:
            self.audit(user, "register_fail")
            return False
        self.users[user] = {
            "email": email,
            "password": hashlib.sha256(password.encode()).hexdigest(),
            "role": role
        }
        self.audit(user, "register")
        return True

    def login(self, user: str, password: str) -> bool:
        if user not in self.users:
            self.audit(user, "login_fail")
            return False
        hashed = hashlib.sha256(password.encode()).hexdigest()
        if self.users[user]["password"] == hashed:
            session_id = str(uuid.uuid4())
            self.sessions[user] = session_id
            self.audit(user, "login")
            return True
        self.audit(user, "login_fail")
        return False

    def assign_role(self, user: str, role: str) -> bool:
        if user not in self.users:
            self.audit(user, "assign_role_fail")
            return False
        self.users[user]["role"] = role
        self.audit(user, "assign_role")
        return True

    def audit(self, user: str, action: str) -> None:
        self.audit_log.append({"user": user, "action": action})
        self.add_debug_info("last_action", action)
        self.add_debug_info("last_user", user)

    def session(self, user: str) -> str:
        return self.sessions.get(user, "")
