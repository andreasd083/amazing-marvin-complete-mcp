"""Entry point: stdio (default) or Streamable HTTP transport.

- stdio: for local use, e.g. as a command-based MCP server in Claude
  Desktop / Claude Code. No network port, no auth layer needed.
- http: for remote use behind a reverse proxy of your choice. A
  bearer-token check (MCP_AUTH_TOKEN or MCP_AUTH_TOKEN_FILE) is enforced
  on ALL paths. Fail closed: without a token the server refuses to start,
  unless MCP_ALLOW_UNAUTHENTICATED=true explicitly opts out — never expose
  an unauthenticated instance to the internet.
"""

from __future__ import annotations

import hmac
import logging

from . import server
from .config import load_settings

# Logging goes to stderr (stdout must stay clean for the stdio transport).
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)


class BearerAuthMiddleware:
    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(auth, expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        await self.app(scope, receive, send)


def check_http_auth(settings) -> None:
    """Fail closed: an unauthenticated HTTP server hands anyone who can
    reach the port full access to the Marvin account."""
    if settings.mcp_auth_token or settings.allow_unauthenticated:
        return
    raise SystemExit(
        "Refusing to start: MCP_TRANSPORT=http but MCP_AUTH_TOKEN is not set.\n"
        "\n"
        "An unauthenticated HTTP server gives anyone who can reach this port\n"
        "full access to your Marvin account (including permanent deletion, if\n"
        "the Full Access token is configured).\n"
        "\n"
        "To fix: generate a long random secret and provide it, e.g.\n"
        "    MCP_AUTH_TOKEN_FILE=/path/to/token-file   (recommended)\n"
        "    MCP_AUTH_TOKEN=<secret>\n"
        "then have your MCP client send it on every request as\n"
        "    Authorization: Bearer <secret>\n"
        "\n"
        "To deliberately run WITHOUT authentication (e.g. a localhost\n"
        "experiment), set MCP_ALLOW_UNAUTHENTICATED=true."
    )


def main() -> None:
    settings = load_settings()
    server.init(settings)
    if settings.transport == "stdio":
        server.mcp.run()  # FastMCP's stdio transport
        return
    if settings.transport != "http":
        raise SystemExit(f"Unknown MCP_TRANSPORT: {settings.transport!r} (use 'stdio' or 'http')")

    check_http_auth(settings)

    import uvicorn

    app = server.mcp.http_app()
    if settings.mcp_auth_token:
        app = BearerAuthMiddleware(app, settings.mcp_auth_token)
    else:
        logging.warning(
            "MCP_ALLOW_UNAUTHENTICATED is set - the HTTP server runs WITHOUT "
            "a bearer-token check. Do not expose it beyond localhost."
        )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
