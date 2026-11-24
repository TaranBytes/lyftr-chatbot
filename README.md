# Lyftr Chat — LLM chatbot with key failover and response caching

A React page talks to a FastAPI backend, the backend calls Gemini through **LiteLLM**, and every conversation is stored in **SQLite**. Under each reply the UI shows the resolution source (`LIVE_INFERENCE` / `LLM_CACHE`), the model, latency in ms and the input/output tokens of that one request.

## 1. Live URLs

| | URL |
|---|---|
| Frontend (Vercel) | https://lyftr-chatbot-khaki.vercel.app |
| Backend (Render) | https://lyftr-chatbot-aurv.onrender.com |
| Health check | https://lyftr-chatbot-aurv.onrender.com/health |
| GitHub | https://github.com/TaranBytes/lyftr-chatbot |

The backend runs on Render's free plan. The first request after a long idle can take 30–60 s while it wakes up (an uptime monitor pings `/health` every 5 minutes to prevent that). Free Render has no persistent disk — see the limitations at the end of section 3.

## 2. Run locally

### With Docker (the `make up` path)

Requirement: Docker Desktop.

```bash
cp .env.example .env      # put your real Gemini keys in LLM_API_KEYS (never commit .env)
make up                   # builds and starts both containers
```

- Frontend: http://localhost:3000
- Backend: http://localhost:8000/health
- Stop: `make down` — chat history stays in the `chat-data` Docker volume mounted at `/data`.

### Without Docker

```bash
# backend (Python 3.12)
cd backend && pip install -r requirements.txt
LLM_API_KEYS=key1,key2 ALLOWED_ORIGINS=http://localhost:3000 DATABASE_URL=sqlite:///./data/app.db \
  uvicorn app.main:create_app_from_env --factory --port 8000

# frontend (Node 20+), in a second terminal
cd frontend && npm install && npm run dev        # http://localhost:3000
```

### Environment variables

| Variable | Meaning | Default / example |
|---|---|---|
| `LLM_API_KEYS` | Comma-separated key pool, at least 2. Ids are `key_1`, `key_2`, ... in this order | required |
| `DATABASE_URL` | SQLite location | `sqlite:////data/app.db` |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins, no trailing slash | `http://localhost:3000` |
| `ENABLE_FAILURE_SIMULATION` | `true` enables the `X-Simulate-Failure` header | `true` |
| `LLM_MODEL` | LiteLLM model name used for every key | `gemini/gemini-3.5-flash-lite` |
| `LLM_TIMEOUT_SECONDS`, `DEFAULT_SYSTEM_PROMPT` | Optional | `30`, a short helpful-assistant prompt |
| `VITE_API_URL` | Frontend only, **build time**: backend URL as seen by the browser | `http://localhost:8000` |

### Tests

```bash
cd backend && pip install -r requirements-dev.txt && pytest     # 58 tests
cd frontend && npm install && npm test                          # 16 tests
```

The backend tests do not mock LiteLLM. They start a local fake OpenAI-compatible server that returns real HTTP 429/500 responses and point the real LiteLLM Router, cache and fallback code at it. They cover the API contract, multi-turn history, cache hit/miss rules, per-request tokens, failover (429/500/502/503), 503 when all keys fail, persistence across restarts, validation, CORS and JSON-only errors.

### Deployment

- **Backend → Render** (Web Service, Docker): Root Directory `backend`, Free instance, health check path `/health`, env vars from the table above with `ENABLE_FAILURE_SIMULATION=true` and `ALLOWED_ORIGINS` set to the exact Vercel URL. `render.yaml` is included.
- **Frontend → Vercel**: Root Directory `frontend`, env var `VITE_API_URL` = the Render URL (no trailing slash). It is baked in at build time, so changing it needs a redeploy.

## 3. Design decisions

```
Browser ──► React (Vite) ──► FastAPI :8000 ──► SQLite /data/app.db
                                  │
                                  └─► LiteLLM Router + cache ──► Gemini key_1 ──(429 / quota / 5xx)──► key_2 ──► ...
```

```
backend/app/    config.py  schemas.py  db.py  llm.py  main.py      (llm.py = everything LiteLLM)
backend/tests/  pytest suite
frontend/src/   App.jsx  api.js  styles.css  App.test.jsx
```

### Key fallback

- `llm.build_router` creates **one LiteLLM model group per key** (`chat-key_1`, `chat-key_2`, ...; the deployment's id is `key_N`, in `LLM_API_KEYS` order). The app always calls `chat-key_1`, and the Router has group-level `fallbacks` to the next keys.
- `num_retries=0` and cooldowns disabled: a failed key is **not** blacklisted, so a simulated or real failure affects only that request and the next request starts at `key_1` again.
- Why not one group with several deployments? I tried it first: LiteLLM's in-group retry kept re-choosing the same deployment and never reached key_2, and the usual fix (cooldowns) blacklists key_1 for later requests, which breaks the "that one request only" rule.
- Any 429, quota error or 5xx moves the request to the next key. If every key fails the API returns `503 {"detail":"all providers unavailable"}`, logs the real error server-side only, and writes **nothing** to history. Responses never contain keys or stack traces (a catch-all handler returns `{"detail":"internal server error"}`).
- `X-Simulate-Failure` is implemented as a LiteLLM deployment hook that raises the matching provider exception right before the provider call, so the **real** fallback code runs. The failure map lives in a `ContextVar`, so concurrent requests cannot affect each other. It is honoured only when `ENABLE_FAILURE_SIMULATION=true`.

### Caching

- LiteLLM's built-in cache (in-memory). The cache key is the model group plus the **full message list** (system prompt + history + new message).
- A hit returns the identical reply and the token usage stored with it, `resolution_source=LLM_CACHE`, `key_id=null`, `fallback_attempts=0`, in a few ms instead of a full provider round trip.
- Consequence of the key: the same first message in a new session is a hit, but the same text later in a conversation is a miss (the history is part of the key). A different message or a different system prompt is never served from cache.
- Requests carrying `X-Simulate-Failure` skip the cache read so failover demos are deterministic. (The PDF doesn't define this order; this is my assumption.)
- One uvicorn worker, so all requests share one cache.

### SQLite session storage

- Stdlib `sqlite3`, WAL mode, schema created at startup (`CREATE TABLE IF NOT EXISTS`). The file path comes from `DATABASE_URL`; in Docker it lives on the named volume at `/data`.
- Tables: `messages` (exactly the PDF's columns, plus an index on `(session_id, id)`) and `sessions` (system prompt per session).
- The history is loaded per `session_id` and prepended to the new message; the client only sends the new message. A user + assistant pair is written in **one transaction after the LLM succeeded**, so a failed request (422/503) never appears in history and a failed first request does not create a session (`GET` stays 404).
- `latency_ms` is measured server-side with `perf_counter` around the history load and the LLM/cache call. Tokens come from that request's LiteLLM `usage`, never cumulative.

### Known limitations

- **Render free has no persistent disk.** The SQLite file and the in-memory cache are wiped on any restart or redeploy (code push, env var change, occasional platform restart). Locally with Docker the volume persists and history survives `docker compose restart backend` (verified). For durable history on Render, use a paid instance with a disk mounted at `/data` (`render.yaml` notes the change).
- The cache is in memory (allowed by the assignment), so it is emptied on every restart.
- No authentication or rate limiting: anyone with the backend URL can use the API and spend the Gemini quota. Rotate the keys after grading.
- LiteLLM is pinned to `1.77.7`: newer versions import proxy-only dependencies as soon as the cache is used.
- The PDF's example model `gemini-2.0-flash` was shut down on 1 Jun 2026, so the model is configurable through `LLM_MODEL` (default `gemini/gemini-3.5-flash-lite`).

## 4. Verification

### Per-message token usage
Every assistant reply in the UI shows `meta-source`, `meta-model`, `meta-latency`, `meta-input-tokens` and `meta-output-tokens` for that request. Send a second message in the same conversation: the input tokens grow (the history is part of the prompt) but they are never a running total.

### Cache hit
The cache key is the whole message list, so use the same **first** message in two sessions: send it, reload the page (new session), send it again → `LLM_CACHE`, a few ms, same reply and tokens, `key_id` null. Repeating the text later in the same conversation is a miss on purpose.

```bash
B=https://lyftr-chatbot-aurv.onrender.com
for s in a1 a2; do curl -s -X POST $B/chat -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"$s\",\"message\":\"Name one planet.\"}"; echo; done
```

### Failover
**From the UI:** use the **Simulate failure** dropdown in the header (or open the page with `?simulate=key_1=429`).

| Option | Result |
|---|---|
| off | normal, footer `key_1 · 0 fallbacks` |
| key_1 fails (429) | normal reply, footer `key_2 · 1 fallbacks` |
| all keys fail | readable red error bubble (HTTP 503) |

**From curl:**
```bash
curl -s -X POST $B/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"f1","message":"Name a colour."}'        # 200, key_id key_2, fallback_attempts 1
curl -s -X POST $B/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"f2","message":"Name a fruit."}'         # 503 {"detail":"all providers unavailable"}
curl -s $B/sessions/f2/messages                              # 404 — the failed request was not stored
```
Header forms: `key_1=429`, `key_1=429,key_2=500`, `*=429`; statuses 429/500/502/503.

### Other checks
```bash
curl -s $B/health                                            # {"status":"ok","keys_configured":2,"cache_enabled":true}
curl -s $B/sessions/<id>/messages                            # history, oldest first, metadata on assistant messages
curl -s -X POST $B/chat -H 'Content-Type: application/json' -d '{"session_id":"x","message":""}'   # 422
```
Persistence locally: `make up`, chat, `docker compose restart backend`, then `GET /sessions/<id>/messages` returns the same history.

## 5. Setup Used

- **Editor:** Visual Studio Code.
- **AI tools:** Claude Code (Claude Sonnet 5.5) inside VS Code for scaffolding, implementation, tests and debugging; Gemini models via Google AI Studio keys as the chatbot's LLM, accessed through LiteLLM.
- **Stack:** FastAPI, LiteLLM 1.77.7, SQLite; React 19 + Vite; pytest, Vitest + Testing Library; Docker / Docker Compose; Render, Vercel, UptimeRobot.

## 6. Bonus features

- **System-prompt field** in the UI (`system-prompt-input`): sets a per-session system prompt, remembered by the backend for later turns.
- **Simulate failure dropdown** in the UI: makes failover and the 503 error visible in the page, which is easier to demo than curl.
- **Extra metadata in the UI:** shows `key_N · N fallbacks` so failover is visible.
- **Uptime monitor** (UptimeRobot on `/health`) to keep the free Render instance awake.
- **Tests that exercise the real LiteLLM** against a fake HTTP server (mutation-checked: disabling caching or fallbacks makes tests fail).
