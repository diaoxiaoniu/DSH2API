from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
import websockets
from config import Settings


class BackendConfigurationError(RuntimeError):
    pass


class BackendRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunOutput:
    text: str
    finish_reason: str = "stop"


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"text", "input_text"}:
            parts.append(str(block.get("text", "")))
        elif kind in {"image_url", "input_image"}:
            parts.append("[image omitted by the OpenAI compatibility bridge]")
    return "\n".join(part for part in parts if part)


def render_messages(messages: list[dict[str, Any]]) -> str:
    """Render an OpenAI transcript into one self-contained Harness prompt.

    A fresh Harness session is used for each HTTP request. This avoids replaying
    a client's full history into a persistent DSH session on every turn.
    """
    lines = [
        "Treat the following as the complete conversation. Reply only to the final user message.",
        "Preserve all system and developer instructions in their given order.",
        "",
    ]
    for item in messages:
        role = str(item.get("role", "user")).upper()
        name = item.get("name")
        label = f"{role} ({name})" if name else role
        lines.extend((f"<{label}>", message_text(item.get("content")), f"</{label}>", ""))
    return "\n".join(lines).rstrip()


def render_user_question(arguments: Any) -> str:
    """Turn Harness's interactive question tool into chat-friendly text."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments.strip()
    if not isinstance(arguments, dict):
        return "请补充完成任务所需的信息。"
    questions = arguments.get("questions")
    if not isinstance(questions, list):
        return "请补充完成任务所需的信息。"

    rendered: list[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        prompt = str(question.get("question") or question.get("header") or "请确认：")
        rendered.append(prompt)
        options = question.get("options")
        if isinstance(options, list):
            for index, option in enumerate(options, 1):
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or f"选项 {index}")
                description = str(option.get("description") or "").strip()
                suffix = f" — {description}" if description else ""
                rendered.append(f"{index}. {label}{suffix}")
        rendered.append("")
    if not rendered:
        return "请补充完成任务所需的信息。"
    rendered.append("请直接回复你的选择；也可以补充具体要求。")
    return "\n".join(rendered).strip()


class HarnessBackend:
    def __init__(self, settings: Settings, factory: Callable[..., Any] | None = None):
        self.settings = settings
        self._factory = factory
        self._harness: Any | None = None
        self._lock = asyncio.Lock()

    def _build(self) -> Any:
        if not self.settings.dsh_home:
            raise BackendConfigurationError(
                "DSH_HOME is empty. Set it to the existing DeepSeek Harness home directory."
            )
        home = Path(self.settings.dsh_home).expanduser()
        if not home.exists():
            raise BackendConfigurationError(f"DSH_HOME does not exist: {home}")
        workspace = Path(self.settings.workspace).expanduser() if self.settings.workspace else home / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        factory = self._factory
        if factory is None:
            try:
                from deepseek_harness import DeepSeekHarness
            except ImportError as exc:
                raise BackendConfigurationError(
                    "deepseek-harness-sdk is not installed; run pip install -r requirements.txt"
                ) from exc
            factory = DeepSeekHarness

        kwargs: dict[str, Any] = {
            "dsh_home": str(home.resolve()),
            "cwd": str(workspace.resolve()),
            "profile": self.settings.profile,
            "provider": self.settings.provider,
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
        }
        if self.settings.reasoning_effort:
            kwargs["reasoning_effort"] = self.settings.reasoning_effort
        return factory(**kwargs)

    def _ensure_harness(self) -> Any:
        if self._harness is None:
            self._harness = self._build()
        return self._harness

    async def run(self, messages: list[dict[str, Any]]) -> RunOutput:
        prompt = render_messages(messages)
        async with self._lock:
            try:
                harness = self._ensure_harness()
                result = await asyncio.to_thread(harness.run, prompt)
            except BackendConfigurationError:
                raise
            except Exception as exc:
                raise BackendRunError(str(exc)) from exc
        text = str(getattr(result, "final_response", "") or "")
        finish = str(getattr(result, "finish_reason", "") or "stop")
        if finish == "error" and not text:
            raise BackendRunError(
                "Harness ended with finish_reason=error and returned no assistant message; "
                "check DSH_HOME, the selected provider/model, and its configured credential"
            )
        if finish == "completed":
            finish = "stop"
        elif finish == "max-tokens":
            finish = "length"
        return RunOutput(text=text, finish_reason=finish)

    async def close(self) -> None:
        harness, self._harness = self._harness, None
        if harness is None:
            return
        close = getattr(harness, "close", None)
        if close:
            await asyncio.to_thread(close)


class RemoteHarnessBackend:
    """Drive a hosted DeepSeek Harness through its authenticated Web gateway."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()

    def _cookie(self) -> str:
        path = Path(self.settings.auth_file).expanduser()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BackendConfigurationError(
                "Remote login has not been imported. Use the bundled Chrome importer first."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BackendConfigurationError(f"Remote auth file is unreadable: {path}") from exc
        cookie = payload.get("cookie") if isinstance(payload, dict) else None
        origin = payload.get("origin") if isinstance(payload, dict) else None
        if not isinstance(cookie, str) or not cookie.strip():
            raise BackendConfigurationError("Remote auth file contains no cookie")
        if origin != self.settings.remote_url:
            raise BackendConfigurationError("Imported login belongs to a different DSH instance")
        return cookie

    async def _rpc(self, endpoint: str, args: dict[str, Any], cookie: str) -> Any:
        rpc_id = f"dsh2api-{endpoint.replace('.', '-')}-{uuid.uuid4().hex}"
        body = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": endpoint,
            "payload": args,
        }
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.post(
                f"{self.settings.remote_url}/api/{endpoint}",
                headers={"Cookie": cookie, "Content-Type": "application/json"},
                json=body,
            )
        if response.status_code in {401, 403} or response.is_redirect:
            raise BackendConfigurationError("LightVela DSH login expired; import the browser login again")
        if response.status_code != 200:
            raise BackendRunError(f"{endpoint} returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            envelope = response.json()
            result = envelope["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise BackendRunError(f"{endpoint} returned an invalid gateway envelope") from exc
        if not result.get("ok"):
            error = result.get("error") or {}
            raise BackendRunError(
                f"{endpoint} failed: {error.get('code', 'unknown')}: {error.get('message', 'unknown error')}"
            )
        return result.get("value")

    async def run(self, messages: list[dict[str, Any]]) -> RunOutput:
        cookie = self._cookie()
        session_id = f"session-{uuid.uuid4()}"
        async with self._lock:
            await self._rpc("session.create", {"sessionId": session_id}, cookie)
            ws_url = self.settings.remote_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
            try:
                async with websockets.connect(
                    f"{ws_url}/api/events.mux",
                    additional_headers={"Cookie": cookie},
                    origin=self.settings.remote_url,
                    open_timeout=20,
                    close_timeout=5,
                    # The hosted gateway may not answer protocol-level pings
                    # while a long Harness tool call is running. Its event
                    # stream is still healthy, so rely on remote_timeout below
                    # instead of websockets' 20-second keepalive timeout.
                    ping_interval=None,
                    max_size=64 * 1024 * 1024,
                ) as socket:
                    await self._rpc("session.prompt", {
                        "sessionId": session_id,
                        "mode": "queue",
                        "content": [{"type": "text", "text": render_messages(messages)}],
                        "clientTimeZone": "Asia/Shanghai",
                    }, cookie)

                    last_text = ""
                    deadline = asyncio.get_running_loop().time() + self.settings.remote_timeout
                    while True:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise BackendRunError("Harness response timed out")
                        raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                        envelope = json.loads(raw)
                        frame = envelope.get("payload") if isinstance(envelope, dict) else None
                        if not isinstance(frame, dict):
                            continue
                        if frame.get("type") != "session/event" or frame.get("sessionId") != session_id:
                            continue
                        event = frame.get("event")
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") == "assistant/message":
                            content = ((event.get("data") or {}).get("message") or {}).get("content") or []
                            last_text = "".join(
                                str(block.get("text", ""))
                                for block in content
                                if isinstance(block, dict) and block.get("type") == "text"
                            )
                        elif event.get("type") == "tool/call":
                            data = event.get("data") or {}
                            if data.get("name") == "ask_user_question":
                                question_text = render_user_question(data.get("arguments"))
                                try:
                                    await self._rpc("session.cancel", {"sessionId": session_id}, cookie)
                                except (BackendRunError, BackendConfigurationError):
                                    pass
                                prefix = f"{last_text.rstrip()}\n\n" if last_text.strip() else ""
                                return RunOutput(f"{prefix}{question_text}", "stop")
                        elif event.get("type") == "turn/end":
                            reason = (event.get("data") or {}).get("reason")
                            turn_reason = reason.get("kind", "completed") if isinstance(reason, dict) else str(reason or "completed")
                            if turn_reason in {"error", "blocked", "aborted"} and not last_text:
                                raise BackendRunError(f"Harness turn ended with {turn_reason}")
                            if not last_text:
                                raise BackendRunError("Harness turn completed without an assistant message")
                            return RunOutput(last_text, "length" if turn_reason == "max-tokens" else "stop")
            except (BackendRunError, BackendConfigurationError):
                raise
            except Exception as exc:
                raise BackendRunError(f"remote Harness connection failed: {exc}") from exc

    async def close(self) -> None:
        return None


def build_backend(settings: Settings) -> HarnessBackend | RemoteHarnessBackend:
    return RemoteHarnessBackend(settings) if settings.remote_url else HarnessBackend(settings)
