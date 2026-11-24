"""Health, normal chat, multi-turn, separate sessions, history endpoint, validation, 404, errors, CORS."""
import pytest

from app.main import create_app


def chat(client, session_id="s-1", message="What is the capital of France?", **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra})


# ---- /health ---------------------------------------------------------------
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


def test_health_without_keys_is_unhealthy(make_client):
    r = make_client(api_keys=[]).get("/health")
    assert r.status_code == 503 and r.json() == {"detail": "no api keys configured"}


# ---- normal chat -----------------------------------------------------------
def test_normal_chat_contract(client):
    r = chat(client)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s-1"
    assert isinstance(body["reply"], str) and body["reply"]
    meta = body["metadata"]
    assert set(meta) >= {"resolution_source", "model", "latency_ms", "input_tokens", "output_tokens", "key_id", "fallback_attempts"}
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "openai/fake-model"
    assert isinstance(meta["latency_ms"], int) and meta["latency_ms"] > 0
    assert isinstance(meta["input_tokens"], int) and meta["input_tokens"] > 0
    assert meta["output_tokens"] == 7
    assert meta["key_id"] == "key_1" and meta["fallback_attempts"] == 0


def test_message_is_trimmed(client):
    body = chat(client, message="   hello   ").json()
    assert "last=hello" in body["reply"]


# ---- multi-turn + separate sessions ---------------------------------------
def test_multi_turn_sends_history_to_llm(client):
    first = chat(client, message="My name is Sahib").json()["reply"]
    second = chat(client, message="What is my name?").json()["reply"]
    assert "n=2" in first  # system + user
    assert "n=4" in second  # system + user + assistant + user  => history was included


def test_sessions_are_separate(client):
    chat(client, "A", "hello from A")
    chat(client, "A", "second from A")
    other = chat(client, "B", "hello from B").json()["reply"]
    assert "n=2" in other  # B knows nothing about A


def test_tokens_are_per_request_not_cumulative(client):
    t1 = chat(client, "A", "one two three").json()["metadata"]["input_tokens"]
    t2 = chat(client, "A", "four five six").json()["metadata"]["input_tokens"]
    # turn 2 contains the history, so it is larger than turn 1 -- but it is NOT t1 + t2 (a running total)
    assert t2 > t1
    # The fake LLM counts one token per word, so turn 2 = turn 1 + assistant reply 1 + new message.
    history = client.get("/sessions/A/messages").json()["messages"]
    assert t2 == t1 + len(history[1]["content"].split()) + len("four five six".split())


def test_equal_length_first_messages_have_same_input_tokens(client):
    a = chat(client, "A", "alpha beta gamma").json()["metadata"]["input_tokens"]
    b = chat(client, "B", "delta kappa omega").json()["metadata"]["input_tokens"]
    assert a == b


def test_custom_system_prompt_is_used_and_remembered(client):
    first = chat(client, "A", "hi", system_prompt="Talk like a pirate").json()["reply"]
    assert "sys=Talk like a pirate" in first
    later = chat(client, "A", "again").json()["reply"]  # no system_prompt sent this time
    assert "sys=Talk like a pirate" in later


# ---- GET /sessions/{id}/messages ------------------------------------------
def test_history_endpoint(client):
    chat(client, "A", "first question")
    chat(client, "A", "second question")
    r = client.get("/sessions/A/messages")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "A"
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]  # oldest first, no system prompt
    assert msgs[0]["content"] == "first question"
    assert "metadata" not in msgs[0]
    meta = msgs[1]["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE" and meta["input_tokens"] > 0 and meta["output_tokens"] == 7


def test_history_unknown_session_404(client):
    r = client.get("/sessions/does-not-exist/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


def test_history_survives_app_restart(make_settings):
    from fastapi.testclient import TestClient

    settings = make_settings()
    with TestClient(create_app(settings)) as first:
        chat(first, "persist", "remember me")
    with TestClient(create_app(settings)) as second:  # brand-new app + new process-level state, same DB file
        msgs = second.get("/sessions/persist/messages").json()["messages"]
    assert [m["content"] for m in msgs][0] == "remember me" and len(msgs) == 2


# ---- validation (422) ------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "s"},  # missing message
        {"message": "hi"},  # missing session_id
        {"session_id": "s", "message": ""},
        {"session_id": "s", "message": "   \n\t "},  # empty after trimming
        {"session_id": "s", "message": "x" * 4001},
        {"session_id": "", "message": "hi"},
        {"session_id": "s" * 129, "message": "hi"},
        {"session_id": 123, "message": "hi"},
        {"session_id": "s", "message": ["not", "a", "string"]},
    ],
)
def test_validation_errors_are_422_and_not_stored(client, payload):
    r = client.post("/chat", json=payload)
    assert r.status_code == 422
    assert "detail" in r.json()
    assert client.get("/sessions/s/messages").status_code == 404  # nothing was stored


def test_boundary_lengths_are_accepted(client):
    assert chat(client, "s" * 128, "x" * 4000).status_code == 200


def test_malformed_json_is_422(client):
    r = client.post("/chat", content="{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and "detail" in r.json()


# ---- JSON errors only ------------------------------------------------------
def test_unknown_route_is_json_not_html(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_only_the_three_endpoints_exist(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_unexpected_error_returns_json_without_stack_trace(make_client, monkeypatch):
    client = make_client()
    monkeypatch.setattr("app.db.Database.get_history", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret internals")))
    r = chat(client)
    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error"}
    assert "secret internals" not in r.text and "Traceback" not in r.text


# ---- CORS ------------------------------------------------------------------
def test_cors_allows_configured_origin_and_custom_header(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-simulate-failure",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "x-simulate-failure" in r.headers["access-control-allow-headers"].lower()


def test_cors_rejects_other_origins(client):
    r = client.post("/chat", json={"session_id": "s", "message": "hi"}, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
