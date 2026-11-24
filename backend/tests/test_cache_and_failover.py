"""Caching behaviour, key failover, X-Simulate-Failure, 503 handling."""
import pytest


def chat(client, session_id="s-1", message="What is the capital of France?", headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})


def sim(value):
    return {"X-Simulate-Failure": value}


# ---- caching ---------------------------------------------------------------
def test_cache_miss_then_hit_in_new_session(client, fake_llm):
    live = chat(client, "s-A").json()
    hit = chat(client, "s-B").json()  # same first message, different (new) session
    assert live["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert hit["metadata"]["resolution_source"] == "LLM_CACHE"
    assert len(fake_llm.calls) == 1  # the hit never reached the provider


def test_cache_hit_returns_identical_reply_and_stored_usage(client):
    live = chat(client, "s-A").json()
    hit = chat(client, "s-B").json()
    assert hit["reply"] == live["reply"]
    for field in ("input_tokens", "output_tokens", "model"):
        assert hit["metadata"][field] == live["metadata"][field]
    assert hit["metadata"]["key_id"] is None
    assert hit["metadata"]["fallback_attempts"] == 0


def test_cache_hit_is_faster_than_live_call(client):
    live = chat(client, "s-A").json()["metadata"]["latency_ms"]
    hit = chat(client, "s-B").json()["metadata"]["latency_ms"]
    assert isinstance(hit, int)
    assert hit < live  # live >= 250 ms (fake provider delay), hit is a few ms


def test_different_message_is_never_served_from_cache(client, fake_llm):
    chat(client, "s-A", "What is the capital of France?")
    other = chat(client, "s-B", "What is the capital of Spain?").json()
    assert other["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert "Spain" in other["reply"]
    assert len(fake_llm.calls) == 2


def test_cache_key_includes_system_prompt(client):
    chat(client, "s-A", "hello", system_prompt="Be formal")
    other = chat(client, "s-B", "hello", system_prompt="Be casual").json()
    assert other["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_cache_key_includes_history(client):
    chat(client, "s-A", "hello")  # live, session A: [sys, hello]
    chat(client, "s-A", "hello")  # session A again: [sys, hello, reply, hello] -> a different full prompt
    r = chat(client, "s-A", "hello").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"  # third time has yet another history


def test_cached_hit_is_stored_in_history_with_its_metadata(client):
    chat(client, "s-A")
    chat(client, "s-B")
    meta = client.get("/sessions/s-B/messages").json()["messages"][1]["metadata"]
    assert meta["resolution_source"] == "LLM_CACHE"


# ---- failover via X-Simulate-Failure ---------------------------------------
@pytest.mark.parametrize("header", ["key_1=429", "key_1=500", "key_1=502", "key_1=503", " key_1 = 429 "])
def test_key_1_failure_fails_over_to_key_2(client, header):
    r = chat(client, headers=sim(header))
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert r.json()["reply"]  # a real answer, not an error wrapped in a 200


def test_simulated_failover_uses_key_2_at_the_provider(client, fake_llm):
    chat(client, headers=sim("key_1=429"))
    assert fake_llm.calls == ["good-key-2"]  # key_1 failed before reaching the provider


def test_simulation_affects_only_that_one_request(client):
    failed_over = chat(client, "s-A", "q1", headers=sim("key_1=429")).json()["metadata"]
    normal = chat(client, "s-A", "q2").json()["metadata"]
    assert failed_over["key_id"] == "key_2"
    assert normal["key_id"] == "key_1" and normal["fallback_attempts"] == 0  # no lingering cooldown


@pytest.mark.parametrize("header", ["key_1=429,key_2=500", "*=429", "*=500", "key_1=503,key_2=502", "*=503"])
def test_all_keys_failing_returns_503(client, header):
    r = chat(client, "s-new", headers=sim(header))
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}


def test_failed_request_is_not_added_to_history(client):
    chat(client, "s-A", "first")  # succeeds -> 2 messages
    r = chat(client, "s-A", "this one fails", headers=sim("*=429"))
    assert r.status_code == 503
    msgs = client.get("/sessions/s-A/messages").json()["messages"]
    assert msgs[0]["content"] == "first"
    assert len(msgs) == 2
    assert all("this one fails" not in m["content"] for m in msgs)


def test_failed_first_request_does_not_create_a_session(client):
    assert chat(client, "s-ghost", headers=sim("*=429")).status_code == 503
    assert client.get("/sessions/s-ghost/messages").status_code == 404


def test_simulated_failure_bypasses_cache(client):
    chat(client, "s-A")  # populates the cache
    r = chat(client, "s-B", headers=sim("*=429"))  # same prompt, but all keys "fail" -> must be 503, not a cache hit
    assert r.status_code == 503


def test_unknown_key_and_garbage_headers_are_ignored(client):
    for header in ("key_9=429", "garbage", "key_1=418", "key_1=", "=429"):
        meta = chat(client, f"s-{header}", message=f"question for {header}", headers=sim(header)).json()["metadata"]  # distinct text -> never a cache hit
        assert meta["key_id"] == "key_1" and meta["fallback_attempts"] == 0


def test_simulation_header_ignored_when_switch_is_off(make_client):
    client = make_client(enable_failure_simulation=False)
    r = chat(client, headers=sim("*=429"))
    assert r.status_code == 200 and r.json()["metadata"]["key_id"] == "key_1"


# ---- real provider errors (the fake server really answers 429 / 500) --------
def test_real_provider_429_fails_over(make_client, fake_llm):
    client = make_client(api_keys=["fail-429-a", "good-key-2"], enable_failure_simulation=False)
    meta = chat(client).json()["metadata"]
    assert meta["key_id"] == "key_2" and meta["fallback_attempts"] == 1
    assert fake_llm.calls == ["fail-429-a", "good-key-2"]


def test_real_provider_500_fails_over(make_client):
    client = make_client(api_keys=["fail-500-a", "good-key-2"], enable_failure_simulation=False)
    assert chat(client).json()["metadata"]["key_id"] == "key_2"


def test_real_provider_failure_on_every_key_is_503(make_client):
    client = make_client(api_keys=["fail-429-a", "fail-500-b"], enable_failure_simulation=False)
    r = chat(client)
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}
    assert client.get("/sessions/s-1/messages").status_code == 404


def test_three_keys_fall_through_in_order(make_client):
    client = make_client(api_keys=["fail-429-a", "fail-500-b", "good-key-3"], enable_failure_simulation=False)
    meta = chat(client).json()["metadata"]
    assert meta["key_id"] == "key_3" and meta["fallback_attempts"] == 2


def test_503_never_leaks_keys_or_internals(make_client):
    client = make_client(api_keys=["fail-429-SECRETKEY", "fail-500-SECRETKEY2"], enable_failure_simulation=False)
    text = chat(client).text
    assert "SECRET" not in text and "Traceback" not in text and "litellm" not in text.lower()


def test_no_keys_configured_chat_is_503(make_client):
    r = chat(make_client(api_keys=[]))
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}
