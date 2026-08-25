import httpx
import pytest

from marvin_mcp.__main__ import BearerAuthMiddleware, check_http_auth
from marvin_mcp.config import Settings


async def dummy_app(scope, receive, send):
    await send(
        {"type": "http.response.start", "status": 200, "headers": []}
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def test_rejects_without_token():
    app = BearerAuthMiddleware(dummy_app, "tst-tok-1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 401
        assert (
            await c.get("/mcp", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401


async def test_accepts_correct_token():
    app = BearerAuthMiddleware(dummy_app, "tst-tok-1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/mcp", headers={"Authorization": "Bearer tst-tok-1"})
        assert resp.status_code == 200


def http_settings(**kwargs) -> Settings:
    return Settings(
        api_token="testapitoken",
        full_access_token=None,
        mcp_auth_token=kwargs.pop("mcp_auth_token", None),
        transport="http",
        **kwargs,
    )


def test_http_without_token_refuses_start():
    # Fail closed - and the message must be pedagogical: say what is
    # missing AND how to set it, plus the deliberate opt-out.
    with pytest.raises(SystemExit) as exc:
        check_http_auth(http_settings())
    message = str(exc.value)
    assert "MCP_AUTH_TOKEN" in message
    assert "MCP_AUTH_TOKEN_FILE" in message
    assert "Authorization: Bearer" in message
    assert "MCP_ALLOW_UNAUTHENTICATED" in message


def test_http_with_token_starts():
    check_http_auth(http_settings(mcp_auth_token="tst-tok-1"))


def test_http_explicit_optout_starts():
    check_http_auth(http_settings(allow_unauthenticated=True))
