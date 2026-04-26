"""API Server platform adapter.

Exposes tau as a REST + WebSocket API for programmatic access.
- POST /chat — send a message and get a response
- GET /sessions — list active sessions
- GET /health — health check
- WS /ws — streaming responses

Install: pip install aiohttp
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from ..adapter import BasePlatformAdapter
from ..config import PlatformConfig, Platform
from ..types import MessageEvent, MessageType, SendResult, SessionSource

logger = logging.getLogger(__name__)


class ApiServerAdapter(BasePlatformAdapter):
    """HTTP/WebSocket API server adapter."""

    MAX_MESSAGE_LENGTH = 100_000  # No real limit for API
    PLATFORM = Platform.API_SERVER

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        self._app: Any = None
        self._runner: Any = None
        self._site: Any = None
        self._port = int(config.extra.get("port", 8741))
        self._host = config.extra.get("host", "127.0.0.1")
        self._pending_responses: dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        """Start the HTTP server."""
        try:
            from aiohttp import web
        except ImportError:
            raise ImportError("aiohttp is required: pip install aiohttp")

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/chat", self._handle_chat)
        app.router.add_get("/sessions", self._handle_sessions)
        app.router.add_get("/ws", self._handle_websocket)

        # API key middleware
        if self.config.api_key:
            @web.middleware
            async def auth_middleware(request, handler):
                if request.path == "/health":
                    return await handler(request)
                api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
                if api_key != self.config.api_key:
                    return web.json_response(
                        {"error": "Unauthorized"}, status=401
                    )
                return await handler(request)

            app.middlewares.append(auth_middleware)

        self._app = app
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        self._running = True
        self._start_time = time.time()
        logger.info("API server started on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
        self._running = False
        logger.info("API server stopped")

    async def send(
        self,
        chat_id: str,
        text: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        parse_mode: str | None = None,
    ) -> SendResult:
        """Resolve a pending response for a chat request."""
        future = self._pending_responses.get(chat_id)
        if future and not future.done():
            future.set_result(text)
            return SendResult(success=True, message_id=chat_id)
        return SendResult(success=True, message_id=str(uuid.uuid4())[:8])

    # ── HTTP handlers ──

    async def _handle_health(self, request: Any) -> Any:
        from aiohttp import web
        return web.json_response({
            "status": "ok",
            "uptime_seconds": round(self.uptime_seconds, 1),
        })

    async def _handle_chat(self, request: Any) -> Any:
        """Handle POST /chat — synchronous chat endpoint."""
        from aiohttp import web

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON body"}, status=400
            )

        text = body.get("message", body.get("text", ""))
        if not text:
            return web.json_response(
                {"error": "Missing 'message' field"}, status=400
            )

        chat_id = body.get("chat_id", str(uuid.uuid4())[:8])
        user_id = body.get("user_id", "api_user")

        source = SessionSource(
            platform="api_server",
            chat_id=chat_id,
            chat_name="API",
            chat_type="api",
            user_id=user_id,
        )

        # Create a future to capture the response
        response_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[chat_id] = response_future

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(uuid.uuid4())[:8],
        )

        # Dispatch to message handler
        await self._dispatch_message(event)

        # Wait for the response (with timeout)
        try:
            response_text = await asyncio.wait_for(response_future, timeout=120)
        except asyncio.TimeoutError:
            response_text = "⚠️ Request timed out (120s)"
        finally:
            self._pending_responses.pop(chat_id, None)

        return web.json_response({
            "response": response_text,
            "chat_id": chat_id,
        })

    async def _handle_sessions(self, request: Any) -> Any:
        """Handle GET /sessions — list active gateway sessions."""
        from aiohttp import web
        # Placeholder — real implementation reads from the runner's session store
        return web.json_response({"sessions": []})

    async def _handle_websocket(self, request: Any) -> Any:
        """Handle WebSocket connections for streaming responses."""
        from aiohttp import web
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    text = data.get("message", "")
                    if text:
                        chat_id = data.get("chat_id", str(uuid.uuid4())[:8])
                        source = SessionSource(
                            platform="api_server",
                            chat_id=chat_id,
                            chat_name="WS",
                            chat_type="websocket",
                            user_id=data.get("user_id", "ws_user"),
                        )

                        response_future: asyncio.Future = asyncio.get_event_loop().create_future()
                        self._pending_responses[chat_id] = response_future

                        event = MessageEvent(
                            text=text,
                            message_type=MessageType.TEXT,
                            source=source,
                        )
                        await self._dispatch_message(event)

                        try:
                            response = await asyncio.wait_for(response_future, timeout=120)
                            await ws.send_json({"response": response, "chat_id": chat_id})
                        except asyncio.TimeoutError:
                            await ws.send_json({"error": "timeout", "chat_id": chat_id})
                        finally:
                            self._pending_responses.pop(chat_id, None)
                except json.JSONDecodeError:
                    await ws.send_json({"error": "Invalid JSON"})
            elif msg.type == web.WSMsgType.ERROR:
                break

        return ws

    async def list_channels(self) -> list[dict[str, Any]]:
        """API server has one virtual channel."""
        return [{
            "chat_id": "api",
            "name": f"API Server ({self._host}:{self._port})",
            "type": "api",
        }]
