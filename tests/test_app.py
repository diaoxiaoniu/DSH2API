from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

import app as app_module
from backend import RunOutput, render_messages, render_user_question


class FakeBackend:
    async def run(self, messages):
        assert messages[-1]["content"] == "hello"
        return RunOutput("world")

    async def close(self):
        return None


def client(monkeypatch):
    monkeypatch.setattr(app_module, "SETTINGS", replace(app_module.SETTINGS, api_key="test-key"))
    monkeypatch.setattr(app_module, "backend", FakeBackend())
    return TestClient(app_module.app)


def auth_headers():
    return {"Authorization": f"Bearer {app_module.SETTINGS.api_key}"}


def anthropic_headers():
    return {
        "x-api-key": app_module.SETTINGS.api_key,
        "anthropic-version": "2023-06-01",
    }


def test_render_messages():
    rendered = render_messages([
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ])
    assert "<SYSTEM>\nbe concise\n</SYSTEM>" in rendered
    assert "<USER>\nhello\n</USER>" in rendered


def test_render_user_question():
    rendered = render_user_question({
        "questions": [{
            "question": "选择数据来源？",
            "options": [
                {"label": "官方 API", "description": "自动获取设备"},
                {"label": "直接 ping"},
            ],
        }]
    })
    assert "选择数据来源？" in rendered
    assert "1. 官方 API — 自动获取设备" in rendered
    assert "2. 直接 ping" in rendered


def test_models(monkeypatch):
    response = client(monkeypatch).get("/v1/models", headers=auth_headers())
    assert response.status_code == 200
    assert any(item["id"] == "deepseek-harness" for item in response.json()["data"])


def test_non_stream(monkeypatch):
    response = client(monkeypatch).post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"model": "deepseek-harness", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "world"


def test_stream(monkeypatch):
    with client(monkeypatch).stream(
        "POST",
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"model": "deepseek-harness", "messages": [{"role": "user", "content": "hello"}], "stream": True},
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    assert any(item["choices"][0]["delta"].get("content") == "world" for item in chunks)


def test_anthropic_non_stream(monkeypatch):
    response = client(monkeypatch).post(
        "/v1/messages",
        headers=anthropic_headers(),
        json={
            "model": "deepseek-harness",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "world"}]
    assert body["stop_reason"] == "end_turn"


def test_anthropic_stream(monkeypatch):
    with client(monkeypatch).stream(
        "POST",
        "/v1/messages",
        headers=anthropic_headers(),
        json={
            "model": "deepseek-harness",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        body = "\n".join(response.iter_lines())
    assert "event: message_start" in body
    assert '"type": "text_delta", "text": "world"' in body
    assert "event: message_stop" in body


def test_anthropic_key_header(monkeypatch):
    response = client(monkeypatch).post(
        "/v1/messages",
        headers={"x-api-key": "wrong"},
        json={
            "model": "deepseek-harness",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 401
    assert response.json()["type"] == "error"
