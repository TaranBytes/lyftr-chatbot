"""Test fixtures.

Instead of mocking LiteLLM we start a tiny fake LLM server (OpenAI-compatible) on localhost and
point LiteLLM at it with LLM_API_BASE. LiteLLM, the Router, the cache and the fallback logic are
therefore all the REAL ones; only the remote model is fake. This is also how we test genuine
upstream 429/500 responses (API keys starting with "fail-429" / "fail-500" make the fake fail).
"""
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

LIVE_DELAY_SECONDS = 0.25  # makes "cache hit is faster than a live call" unambiguous


class FakeLLM:
    def __init__(self):
        self.calls: list[str] = []  # API key of every request that reached the "provider"
        self.app = FastAPI()

        @self.app.post("/v1/chat/completions")
        async def completions(request: Request):
            body = await request.json()
            key = request.headers.get("authorization", "").removeprefix("Bearer ")
            self.calls.append(key)
            if key.startswith("fail-429"):
                return JSONResponse(status_code=429, content={"error": {"message": "quota exceeded", "type": "rate_limit"}})
            if key.startswith("fail-500"):
                return JSONResponse(status_code=500, content={"error": {"message": "boom", "type": "server_error"}})
            time.sleep(LIVE_DELAY_SECONDS)
            messages = body["messages"]
            prompt_tokens = sum(len(m["content"].split()) for m in messages)
            reply = f"sys={messages[0]['content']}|n={len(messages)}|last={messages[-1]['content']}"
            return JSONResponse(
                {
                    "id": "fake",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 7, "total_tokens": prompt_tokens + 7},
                }
            )


@pytest.fixture(scope="session")
def _fake_server():
    fake = FakeLLM()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    fake.url = f"http://127.0.0.1:{port}/v1"
    server = uvicorn.Server(uvicorn.Config(fake.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield fake
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def fake_llm(_fake_server) -> FakeLLM:
    _fake_server.calls.clear()
    return _fake_server


@pytest.fixture
def make_settings(tmp_path, fake_llm):
    def _make(**overrides) -> Settings:
        base = dict(
            api_keys=["good-key-1", "good-key-2"],
            llm_model="openai/fake-model",
            database_url=f"sqlite:///{tmp_path}/test.db",
            allowed_origins=["http://localhost:3000"],
            enable_failure_simulation=True,
            llm_api_base=fake_llm.url,
        )
        base.update(overrides)
        return Settings(**base)

    return _make


@pytest.fixture
def make_client(make_settings):
    clients = []

    def _make(**overrides) -> TestClient:
        client = TestClient(create_app(make_settings(**overrides)), raise_server_exceptions=False)
        client.__enter__()  # runs the lifespan -> creates the SQLite schema
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture(autouse=True)
def _reset_litellm_logging_worker():
    """LiteLLM keeps one global asyncio queue bound to the first event loop that used it.
    Every TestClient has its own event loop, so reset it between tests. (Production runs a
    single event loop, so this only matters for the test-suite.)"""
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    import litellm

    GLOBAL_LOGGING_WORKER._queue = None
    GLOBAL_LOGGING_WORKER._worker_task = None
    # Each test builds its own app + Router; LiteLLM caps global callbacks at 30, so start clean.
    for name in ("callbacks", "input_callback", "success_callback", "failure_callback", "_async_success_callback", "_async_failure_callback"):
        setattr(litellm, name, [])
    litellm.cache = None  # the response cache is process-global; every test starts with an empty one
    yield
