from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from backend import BackendConfigurationError, BackendRunError, build_backend
from config import SETTINGS


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "deepseek-harness"
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "deepseek-harness"
    max_tokens: int = 8192
    messages: list[dict[str, Any]] = Field(min_length=1)
    system: str | list[dict[str, Any]] | None = None
    stream: bool = False


backend = build_backend(SETTINGS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await backend.close()


app = FastAPI(title="dsh2api", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def openai_error(message: str, status: int, error_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": error_type, "param": None, "code": status}},
    )


def anthropic_error(message: str, status: int, error_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path == "/v1/messages":
        error_type = "authentication_error" if exc.status_code == 401 else "invalid_request_error"
        return anthropic_error(str(exc.detail), exc.status_code, error_type)
    if request.url.path.startswith("/v1/"):
        return openai_error(str(exc.detail), exc.status_code)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path == "/v1/messages":
        return anthropic_error(f"Request validation failed: {exc.errors()}", 422)
    if request.url.path.startswith("/v1/"):
        return openai_error(f"Request validation failed: {exc.errors()}", 422)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


def require_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    if not SETTINGS.api_key:
        raise HTTPException(503, "DSH2API_KEY is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if x_api_key == SETTINGS.api_key:
        return
    if scheme.lower() != "bearer" or token != SETTINGS.api_key:
        raise HTTPException(401, "Invalid API key")


@app.get("/health")
async def health() -> dict[str, Any]:
    remote = bool(SETTINGS.remote_url)
    return {
        "ok": remote or bool(SETTINGS.dsh_home),
        "backend": "remote-webui" if remote else "deepseek-harness-sdk",
        "configured": remote or bool(SETTINGS.dsh_home),
    }


class AuthImport(BaseModel):
    origin: str
    cookie: str = Field(min_length=1)


@app.post("/admin/auth/import", dependencies=[Depends(require_key)])
async def import_remote_auth(payload: AuthImport, request: Request):
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "Remote login can only be imported from this computer")
    if not SETTINGS.remote_url:
        raise HTTPException(400, "DSH_REMOTE_URL is not configured")
    if payload.origin.rstrip("/") != SETTINGS.remote_url:
        raise HTTPException(400, "The browser tab belongs to a different DSH instance")
    path = Path(SETTINGS.auth_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"origin": SETTINGS.remote_url, "cookie": payload.cookie}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return {"ok": True, "origin": SETTINGS.remote_url}


@app.get("/v1/models", dependencies=[Depends(require_key)])
async def models() -> dict[str, Any]:
    now = int(time.time())
    aliases = ["deepseek-harness", "deepseek-chat", "deepseek-reasoner", SETTINGS.model]
    unique = list(dict.fromkeys(aliases))
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": now, "owned_by": "deepseek-harness"}
            for model in unique
        ],
    }


def completion_payload(request: ChatRequest, text: str, finish_reason: str, completion_id: str, created: int):
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def sse(request: ChatRequest, text: str, finish_reason: str, completion_id: str, created: int) -> AsyncIterator[str]:
    base = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": request.model}
    first = {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
    # The SDK commits assistant text at the end of a Harness turn. Chunk the
    # committed text for OpenAI client compatibility; this is not token-live.
    for start in range(0, len(text), 64):
        chunk = {**base, "choices": [{"index": 0, "delta": {"content": text[start:start + 64]}, "finish_reason": None}]}
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    if request.stream_options and request.stream_options.get("include_usage"):
        final["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions", dependencies=[Depends(require_key)])
async def chat_completions(request: ChatRequest):
    try:
        output = await backend.run(request.messages)
    except BackendConfigurationError as exc:
        return openai_error(str(exc), 503, "server_error")
    except BackendRunError as exc:
        return openai_error(f"DeepSeek Harness run failed: {exc}", 502, "server_error")

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    if request.stream:
        return StreamingResponse(
            sse(request, output.text, output.finish_reason, completion_id, created),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return completion_payload(request, output.text, output.finish_reason, completion_id, created)


def anthropic_messages(request: AnthropicMessagesRequest) -> list[dict[str, Any]]:
    messages = list(request.messages)
    if request.system is not None:
        messages.insert(0, {"role": "system", "content": request.system})
    return messages


def anthropic_payload(
    request: AnthropicMessagesRequest,
    text: str,
    finish_reason: str,
    message_id: str,
) -> dict[str, Any]:
    stop_reason = "max_tokens" if finish_reason == "length" else "end_turn"
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": request.model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


async def anthropic_sse(
    request: AnthropicMessagesRequest,
    text: str,
    finish_reason: str,
    message_id: str,
) -> AsyncIterator[str]:
    start = {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": request.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    yield f"event: message_start\ndata: {json.dumps(start, ensure_ascii=False)}\n\n"
    block_start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    yield f"event: content_block_start\ndata: {json.dumps(block_start, ensure_ascii=False)}\n\n"
    for offset in range(0, len(text), 64):
        delta = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text[offset:offset + 64]},
        }
        yield f"event: content_block_delta\ndata: {json.dumps(delta, ensure_ascii=False)}\n\n"
    block_stop = {"type": "content_block_stop", "index": 0}
    yield f"event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n"
    stop_reason = "max_tokens" if finish_reason == "length" else "end_turn"
    message_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": 0},
    }
    yield f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n"
    yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'


@app.post("/v1/messages", dependencies=[Depends(require_key)])
async def messages(request: AnthropicMessagesRequest):
    try:
        output = await backend.run(anthropic_messages(request))
    except BackendConfigurationError as exc:
        return anthropic_error(str(exc), 503, "api_error")
    except BackendRunError as exc:
        return anthropic_error(f"DeepSeek Harness run failed: {exc}", 502, "api_error")

    message_id = f"msg_{uuid.uuid4().hex}"
    if request.stream:
        return StreamingResponse(
            anthropic_sse(request, output.text, output.finish_reason, message_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return anthropic_payload(request, output.text, output.finish_reason, message_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=SETTINGS.host, port=SETTINGS.port, reload=False)
