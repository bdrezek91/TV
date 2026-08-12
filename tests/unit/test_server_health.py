from starlette.testclient import TestClient

from tradingview_mcp.server import mcp


def test_health_endpoint_is_available_without_mcp_session() -> None:
    with TestClient(mcp.streamable_http_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
