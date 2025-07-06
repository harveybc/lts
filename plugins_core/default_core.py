"""
Default Core Plugin for LTS (Live Trading System)

This plugin is responsible for:
1. Starting the FastAPI server
2. Running the main trading loop
3. Coordinating all other plugins
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, FastAPI
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.plugin_base import PluginBase
from app.database import get_db, User

# Define the dependency function outside the class
async def get_current_user(security: HTTPAuthorizationCredentials = Depends(HTTPBearer())):
    """
    Dependency to get the current user from a token.
    This is a simplified placeholder.
    """
    if security and security.credentials == "validtoken":
        return {"username": "testuser"}
    else:
        raise HTTPException(status_code=403, detail="Invalid token or authentication scheme")

class DataItem(BaseModel):
    name: str
    price: float

class CorePlugin(PluginBase):
    def __init__(self):
        super().__init__()
        self.name = "Core"
        self.version = "0.1.0"
        self.description = "Core plugin providing essential API endpoints."
        self.router = APIRouter()
        self.plugins = {}
        self._register_routes()

    def initialize(self, plugins: dict):
        """
        Initializes the plugin. The app is created by the main orchestrator.
        """
        self.plugins = plugins

    def _register_routes(self):
        """
        Registers the API routes for this plugin.
        """
        self.router.add_api_route("/api/v1/status", self.get_status, methods=["GET"])
        self.router.add_api_route("/api/v1/secure", self.get_secure_data, methods=["GET"], dependencies=[Depends(get_current_user)])
        self.router.add_api_route("/api/v1/data", self.process_data, methods=["POST"], dependencies=[Depends(get_current_user)])

    async def add_security_headers(self, request: Request, call_next: RequestResponseEndpoint):
        """
        Middleware to add security headers to all responses.
        """
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response

    async def handle_500_error(self, request: Request, exc: Exception):
        """
        Handles internal server errors, returning a JSON response.
        """
        logging.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal Server Error: {exc}"},
        )

    @staticmethod
    def get_db() -> Session:
        """
        Provides a database session dependency.
        """
        # This is a placeholder and will be mocked in tests.
        # In a real app, this would fetch a session from the database.
        db = next(get_db())
        yield db

    async def get_status(self):
        """
        Returns the status of the API.
        """
        return {"status": "ok"}

    async def get_secure_data(self, current_user: dict = Depends(get_current_user)):
        """
        An example secure endpoint that requires authentication.
        """
        return {"status": "ok", "user": current_user['username']}

    async def process_data(self, data: DataItem, current_user: dict = Depends(get_current_user)):
        """
        Processes incoming data, requires authentication.
        This is a placeholder for more complex logic.
        """
        # Use a (mocked) real function to allow for exception testing
        return self.some_real_function(data)

    def some_real_function(self, data: DataItem):
        """Placeholder for a real function that might be patched in tests."""
        # In a real scenario, this might interact with other plugins or services
        # For example, using the 'aaa' plugin if it exists
        if 'aaa' in self.plugins:
            self.plugins['aaa'].audit_action(f"User {data.name} processed data.")
        return {"message": f"Data for {data.name} processed successfully."}


# Create a singleton instance of the plugin for the app factory to use
core_plugin_instance = CorePlugin()

def create_app() -> FastAPI:
    """
    Application factory to create a fresh FastAPI instance.
    This is essential for testing to ensure a clean state for each test.
    """
    app = FastAPI()
    app.include_router(core_plugin_instance.router)
    app.add_middleware(BaseHTTPMiddleware, dispatch=core_plugin_instance.add_security_headers)
    app.add_exception_handler(Exception, core_plugin_instance.handle_500_error)
    return app
