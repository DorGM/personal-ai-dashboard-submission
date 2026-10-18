"""Security response headers (B-07): Content-Security-Policy and anti-framing on every HTTP response.

The pages load no scripts (no HTMX or other JS is used yet) and no inline styles; CSS comes
from /static and the favicon is a data: URI, so the policy needs nothing beyond 'self' and
`img-src data:`. Charts are inline SVG markup, which CSP does not restrict.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'self'")
HEADERS = {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}


class SecurityHeadersMiddleware:
    """Add HEADERS to every HTTP response (pure ASGI, so streamed responses are not buffered)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
