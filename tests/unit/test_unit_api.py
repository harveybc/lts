"""
test_unit_api.py

Unit tests for the Web API components of the LTS application.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

# Import the singleton instance of the core plugin and its data model
from plugins_core.default_core import core_plugin_instance, DataItem, CorePlugin, get_current_user, create_app

@pytest.fixture(scope="function")
def client():
    """
    Provides a FastAPI TestClient for API testing.
    This fixture is function-scoped to ensure clean dependency overrides for each test.
    """
    app = create_app()

    # Mock the database dependency for all tests in this module
    mock_db_session = MagicMock()
    app.dependency_overrides[CorePlugin.get_db] = lambda: mock_db_session

    # Initialize the core plugin with a mock AAA plugin
    core_plugin_instance.initialize(plugins={'aaa': MagicMock()})

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    
    # Clear all overrides after each test
    app.dependency_overrides = {}

def test_api_secure_endpoint_authenticated(client):
    """
    Tests that a secure endpoint returns 200 OK with a valid token.
    (U-018: API endpoint authentication)
    """
    # Override the user dependency specifically for this test
    client.app.dependency_overrides[get_current_user] = lambda: {"username": "testuser"}
    response = client.get("/api/v1/secure", headers={"Authorization": "Bearer validtoken"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "user": "testuser"}

def test_api_secure_endpoint_no_token(client):
    """
    Tests that a secure endpoint returns 403 Forbidden without a token.
    (U-018: API endpoint authentication)
    """
    # No override needed; we are testing the actual dependency
    response = client.get("/api/v1/secure")
    assert response.status_code == 403

def test_api_input_validation_valid(client):
    """
    Tests that an endpoint with input validation passes with valid data.
    (U-019: API input validation)
    """
    client.app.dependency_overrides[get_current_user] = lambda: {"username": "testuser"}
    with patch.object(core_plugin_instance, 'some_real_function') as mock_func:
        mock_func.return_value = {"message": "success"}
        valid_data = {"name": "test", "price": 10.0}
        response = client.post("/api/v1/data", json=valid_data, headers={"Authorization": "Bearer validtoken"})
        assert response.status_code == 200
        assert response.json() == {"message": "success"}

def test_api_input_validation_invalid(client):
    """
    Tests that an endpoint with input validation returns 422 with invalid data.
    (U-019: API input validation)
    """
    client.app.dependency_overrides[get_current_user] = lambda: {"username": "testuser"}
    invalid_data = {"name": "test", "price": "not-a-float"}
    response = client.post("/api/v1/data", json=invalid_data, headers={"Authorization": "Bearer validtoken"})
    assert response.status_code == 422

def test_api_response_formatting(client):
    """
    Tests that the API returns responses in the expected JSON format.
    (U-020: API response formatting)
    """
    response = client.get("/api/v1/status")
    assert response.headers["content-type"] == "application/json"
    assert isinstance(response.json(), dict)

def test_api_error_handling(client):
    """
    Tests that the API handles internal errors gracefully and returns a 500 status.
    (U-021: API error handling)
    """
    client.app.dependency_overrides[get_current_user] = lambda: {"username": "testuser"}
    # Patch the real function on the core plugin instance to raise an unhandled exception
    with patch.object(core_plugin_instance, 'some_real_function', side_effect=Exception("Internal Server Error")):
        valid_data = {"name": "test", "price": 10.0}
        response = client.post("/api/v1/data", json=valid_data, headers={"Authorization": "Bearer validtoken"})
        assert response.status_code == 500
        response_json = response.json()
        assert "detail" in response_json
        assert "Internal Server Error" in response_json["detail"]

def test_api_security_headers(client):
    """
    Tests that the API response includes recommended security headers.
    (U-024: API security headers)
    """
    response = client.get("/api/v1/status")
    assert "x-content-type-options" in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" in response.headers
    assert response.headers["x-frame-options"] == "DENY"
