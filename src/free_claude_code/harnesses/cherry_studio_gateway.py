"""Cherry Studio API Gateway: loopback OpenAI-compatible proxy for FCC."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger

from free_claude_code.cli.server_socket import ServerSockets
from free_claude_code.config.server_urls import local_proxy_root_url

DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 23333
_GATEWAY_HOST_ENV = "CHERRY_STUDIO_GATEWAY_HOST"
_GATEWAY_PORT_ENV = "CHERRY_STUDIO_GATEWAY_PORT"
_DEFAULT_TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# Format conversion: OpenAI chat/completions <-> Anthropic messages
# ---------------------------------------------------------------------------

def _extract_system(body: Mapping[str, Any]) -> str | None:
    messages = body.get("messages") or []
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, Mapping) or msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, Mapping) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
    return "\n\n".join(parts) if parts else None


def _strip_system(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    messages = body.get("messages") or []
    return [
        msg for msg in messages
        if isinstance(msg, Mapping) and msg.get("role") != "system"
    ]


def openai_to_anthropic(body: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat-completions request into an Anthropic request."""
    result: dict[str, Any] = {
        "model": str(body.get("model") or ""),
        "messages": _strip_system(body),
    }
    system = _extract_system(body)
    if system:
        result["system"] = system
    if "max_tokens" in body:
        result["max_tokens"] = body["max_tokens"]
    elif "max_completion_tokens" in body:
        result["max_tokens"] = body["max_completion_tokens"]
    else:
        result["max_tokens"] = 1024
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "top_p" in body:
        result["top_p"] = body["top_p"]
    if "stop" in body:
        stop = body["stop"]
        result["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    if "tools" in body:
        result["tools"] = body["tools"]
    return result


def _finish_reason(stop_reason: str | None) -> str:
    if stop_reason == "end_turn":
        return "stop"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "stop_sequence":
        return "stop"
    return "stop"


def anthropic_to_openai(data: Mapping[str, Any], *, chatcmpl_id: str, created: int, model: str) -> dict[str, Any]:
    """Translate an Anthropic messages response into an OpenAI chat completion."""
    blocks = data.get("content") or []
    text = "".join(
        str(b.get("text", ""))
        for b in blocks
        if isinstance(b, Mapping) and b.get("type") == "text"
    )
    usage = data.get("usage") or {}
    return {
        "id": chatcmpl_id,
        "object": "chat.completion",
        "created": created,
        "model": str(data.get("model") or model),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _finish_reason(data.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
            "completion_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0),
        },
    }


def _openai_chunk(*, chatcmpl_id: str, created: int, model: str, content: str | None = None, finish_reason: str | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if finish_reason is not None:
        delta["finish_reason"] = finish_reason
    if not delta:
        delta["role"] = "assistant"
    return {
        "id": chatcmpl_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


async def stream_anthropic_to_openai(
    response: httpx.Response,
    *,
    chatcmpl_id: str,
    created: int,
    model: str,
) -> AsyncIterator[str]:
    """Yield OpenAI SSE chunks from an Anthropic SSE stream."""
    emitted_finish = False
    async for line in response.aiter_lines():
        if not line or not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if not data_str:
            continue
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        etype = event.get("type")
        if etype == "message_start":
            yield f"data: {json.dumps(_openai_chunk(chatcmpl_id=chatcmpl_id, created=created, model=model))}\n\n"
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                yield f"data: {json.dumps(_openai_chunk(chatcmpl_id=chatcmpl_id, created=created, model=model, content=delta.get("text", "")))}\n\n"
        elif etype == "message_delta":
            delta = event.get("delta") or {}
            finish = _finish_reason(delta.get("stop_reason"))
            if finish:
                emitted_finish = True
            yield f"data: {json.dumps(_openai_chunk(chatcmpl_id=chatcmpl_id, created=created, model=model, finish_reason=finish or None))}\n\n"
        elif etype == "message_stop":
            if not emitted_finish:
                yield f"data: {json.dumps(_openai_chunk(chatcmpl_id=chatcmpl_id, created=created, model=model, finish_reason='stop'))}\n\n"
            yield "data: [DONE]\n\n"
            return
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Gateway runtime
# ---------------------------------------------------------------------------

class CherryStudioGateway:
    """Loopback-only OpenAI-compatible gateway that proxies to FCC."""

    def __init__(self, settings, *, host=None, port=None):
        self._settings = settings
        self._host = host or os.environ.get(_GATEWAY_HOST_ENV) or DEFAULT_GATEWAY_HOST
        try:
            self._port = int(port or os.environ.get(_GATEWAY_PORT_ENV) or DEFAULT_GATEWAY_PORT)
        except ValueError:
            self._port = DEFAULT_GATEWAY_PORT
        self._sockets = None
        self._server = None
        self._thread = None
        self._thread_ready = None
        self._app = self._build_app()
        self._http_client = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _fcc_url(self) -> str:
        return local_proxy_root_url(self._settings)

    def _token(self) -> str:
        return str(self._settings.proxy_auth_token or "")

    def _check_auth(self, auth: str | None) -> bool:
        token = self._token()
        if not token:
            return True
        if not auth:
            return False
        expected = f"Bearer {token}"
        return auth == expected

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="FCC Cherry Studio Gateway")

        @app.get("/health")
        async def health():
            return {"status": "ok", "gateway": "cherry-studio", "fcc": self._fcc_url()}

        @app.get("/")
        async def root():
            return {"gateway": "cherry-studio", "fcc": self._fcc_url()}

        @app.get("/models")
        async def list_models(request: Request, auth: str | None = None):
            if not self._check_auth(auth):
                raise HTTPException(status_code=401, detail="Invalid or missing gateway token")
            client = self._client()
            try:
                resp = await client.get(f"{self._fcc_url()}/v1/models")
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"FCC unreachable: {exc}")
            return JSONResponse(content=resp.json(), status_code=resp.status_code)

        @app.post("/v1/messages")
        async def create_messages(request: Request, auth: str | None = None):
            if not self._check_auth(auth):
                raise HTTPException(status_code=401, detail="Invalid or missing gateway token")
            body = await request.json()
            client = self._client()
            try:
                resp = await client.post(f"{self._fcc_url()}/v1/messages", json=body)
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"FCC unreachable: {exc}")
            return JSONResponse(content=resp.json(), status_code=resp.status_code)

        @app.post("/v1/responses")
        async def create_responses(request: Request, auth: str | None = None):
            if not self._check_auth(auth):
                raise HTTPException(status_code=401, detail="Invalid or missing gateway token")
            body = await request.json()
            client = self._client()
            try:
                resp = await client.post(f"{self._fcc_url()}/v1/responses", json=body)
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"FCC unreachable: {exc}")
            return JSONResponse(content=resp.json(), status_code=resp.status_code)

        @app.post("/v1/chat/completions")
        async def create_chat_completions(request: Request, auth: str | None = None):
            if not self._check_auth(auth):
                raise HTTPException(status_code=401, detail="Invalid or missing gateway token")
            try:
                body = await request.json()
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON body")
            anthropic_body = openai_to_anthropic(body)
            model = str(body.get("model") or "")
            chatcmpl_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            created = int(time.time())
            client = self._client()
            try:
                if body.get("stream"):
                    resp = await client.post(
                        f"{self._fcc_url()}/v1/messages",
                        json=anthropic_body,
                        headers={"Accept": "text/event-stream"},
                    )
                    if resp.status_code >= 400:
                        raise HTTPException(status_code=resp.status_code, detail=resp.text)
                    return StreamingResponse(
                        stream_anthropic_to_openai(resp, chatcmpl_id=chatcmpl_id, created=created, model=model),
                        media_type="text/event-stream",
                    )
                resp = await client.post(f"{self._fcc_url()}/v1/messages", json=anthropic_body)
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"FCC unreachable: {exc}")
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            return JSONResponse(
                content=anthropic_to_openai(resp.json(), chatcmpl_id=chatcmpl_id, created=created, model=model)
            )

        return app

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        return self._http_client

    def _run_serve(self) -> None:
        """Run uvicorn.serve() in this worker thread.

        uvicorn's ``serve()`` must run on its own event loop. Scheduling it as
        a task on the main FCC loop leaves the server registered but unable to
        accept connections on Windows, so the gateway mirrors the main server
        and owns a dedicated worker thread with a fresh event loop.
        """
        try:
            asyncio.run(self._server.serve(sockets=self._sockets.sockets))
        except Exception as exc:
            logger.error("Cherry Studio gateway worker crashed: {!r}", exc)
        finally:
            self._thread_ready.set()

    async def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.info("Cherry Studio gateway already running on {}:{}", self._host, self._port)
            return
        logger.info("Cherry Studio gateway start() entered for {}:{}", self._host, self._port)
        try:
            self._sockets = ServerSockets.reserve(self._host, self._port)
            logger.info("Cherry Studio gateway bound sockets for {}:{} (count={})", self._host, self._port, len(self._sockets.sockets))
        except OSError as exc:
            logger.warning("Cherry Studio gateway could not bind {}:{}: {}", self._host, self._port, exc)
            return
        except Exception as exc:
            logger.error("Cherry Studio gateway reserve raised unexpected {!r}", exc)
            return
        try:
            config = uvicorn.Config(
                self._app,
                host=self._host,
                port=self._port,
                log_level="warning",
                access_log=False,
                # FCC already owns the logging system (bootstrap.configure_logging).
                # Letting uvicorn reconfigure it raises ValueError("Unable to
                # configure formatter 'default'") inside the running server, so
                # hand it a no-op log config and reuse the existing handlers.
                log_config=None,
            )
            self._server = uvicorn.Server(config)
            self._thread_ready = threading.Event()
            self._thread = threading.Thread(
                target=self._run_serve,
                name="fcc-cherry-studio-gateway",
                daemon=True,
            )
            self._thread.start()
            logger.info("Cherry Studio gateway worker started for {}:{} (sockets={})", self._host, self._port, bool(self._sockets and self._sockets.sockets))
        except Exception as exc:
            logger.error("Cherry Studio gateway start() failed after bind: {!r}", exc)
            if self._sockets is not None:
                self._sockets.close()
                self._sockets = None
            self._server = None
            self._thread = None

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._sockets is not None:
            self._sockets.close()
            self._sockets = None
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._server = None
        self._thread = None
        self._thread_ready = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.running,
            "host": self._host,
            "port": self._port,
            "url": f"http://{self._host}:{self._port}",
            "fcc": self._fcc_url(),
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def gateway_host_port(settings) -> tuple[str, int]:
    host = os.environ.get(_GATEWAY_HOST_ENV) or DEFAULT_GATEWAY_HOST
    try:
        port = int(os.environ.get(_GATEWAY_PORT_ENV) or DEFAULT_GATEWAY_PORT)
    except ValueError:
        port = DEFAULT_GATEWAY_PORT
    return host, port


def build_gateway(settings) -> CherryStudioGateway:
    host, port = gateway_host_port(settings)
    return CherryStudioGateway(settings, host=host, port=port)


__all__ = [
    "CherryStudioGateway",
    "build_gateway",
    "gateway_host_port",
    "openai_to_anthropic",
    "anthropic_to_openai",
    "stream_anthropic_to_openai",
    "DEFAULT_GATEWAY_HOST",
    "DEFAULT_GATEWAY_PORT",
]
