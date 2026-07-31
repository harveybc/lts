import json

import pytest

from app.capital_demo_lab import (
    CapitalDemoClient,
    CapitalDemoConfig,
    CapitalDemoError,
    CapitalDemoOlap,
)
from plugins_broker.capital_demo_broker import CapitalDemoBroker


class Response:
    def __init__(self, payload, *, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload


class Session:
    def __init__(self):
        self.headers = {}
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if url.endswith("/session"):
            return Response({}, headers={"CST": "cst", "X-SECURITY-TOKEN": "sec"})
        if url.endswith("/accounts"):
            return Response({"accounts": [{"accountId": "ABC", "accountName": "Demo"}]})
        if url.endswith("/positions"):
            return Response({"positions": []})
        if url.endswith("/workingorders"):
            return Response({"workingOrders": []})
        if url.endswith("/markets"):
            return Response(
                {
                    "markets": [
                        {
                            "epic": "BTCUSD",
                            "instrumentName": "Bitcoin",
                            "marketStatus": "TRADEABLE",
                            "bid": 99,
                            "offer": 101,
                        }
                    ]
                }
            )
        raise AssertionError(url)


def _config(tmp_path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "lts.capital.demo_lab_config.v1",
        "environment": "demo",
        "mode": "get_only",
        "base_url": "https://demo-api-capital.backend-capital.com",
        "database_path": str(tmp_path / "capital.sqlite"),
        "orders": {"enabled": False},
        "secrets": {
            "api_key_env": "CAPITAL_DEMO_API_KEY",
            "identifier_env": "CAPITAL_DEMO_IDENTIFIER",
            "password_env": "CAPITAL_DEMO_PASSWORD",
        },
        "market_search_terms": ["Bitcoin"],
    }
    payload.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return CapitalDemoConfig.load(path)


def test_config_is_strictly_demo_get_only(tmp_path):
    config = _config(tmp_path)
    assert config.credentials(
        {
            "CAPITAL_DEMO_API_KEY": "key",
            "CAPITAL_DEMO_IDENTIFIER": "id",
            "CAPITAL_DEMO_PASSWORD": "password",
        }
    ) == ("key", "id", "password")
    with pytest.raises(CapitalDemoError, match="demo-only"):
        _config(tmp_path / "live", environment="live")
    with pytest.raises(CapitalDemoError, match="GET-only"):
        _config(tmp_path / "write", mode="execute")
    with pytest.raises(CapitalDemoError, match="disabled"):
        _config(tmp_path / "orders", orders={"enabled": True})


def test_client_only_posts_authentication_and_gets_data(tmp_path):
    config = _config(tmp_path)
    session = Session()
    client = CapitalDemoClient("key", "id", "password", session=session)
    snapshot = client.snapshot(config.search_terms)
    assert len(snapshot["accounts"]) == 1
    assert snapshot["positions"] == []
    assert snapshot["working_orders"] == []
    assert snapshot["markets"]["Bitcoin"][0]["epic"] == "BTCUSD"
    assert [method for method, _, _ in session.requests] == [
        "POST",
        "GET",
        "GET",
        "GET",
        "GET",
    ]
    assert session.headers["CST"] == "cst"
    assert session.headers["X-SECURITY-TOKEN"] == "sec"

    store = CapitalDemoOlap(config.database_path)
    try:
        result = store.record(snapshot, client.probes)
        assert result["orders_submitted"] == 0
        assert store.report()["latest_session"]["status"] == "complete"
        assert b"ABC" not in config.database_path.read_bytes()
    finally:
        store.close()


def test_mutations_are_explicitly_disabled():
    broker = CapitalDemoBroker({})
    for result in (
        broker.open_order(),
        broker.modify_order(),
        broker.close_order(),
        broker.execute_order(),
    ):
        assert result["success"] is False
        assert "disabled" in result["error"]
