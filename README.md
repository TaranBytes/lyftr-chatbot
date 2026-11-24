# Lyftr Chat

A small full-stack chatbot: a React page talks to a FastAPI backend, the backend calls Gemini through **LiteLLM**, and every conversation is stored in **SQLite**. Under each reply the UI shows where the answer came from, the model, latency, and input/output tokens.

| | URL |
|---|---|
| Frontend (Vercel) | https://lyftr-chatbot-khaki.vercel.app |
| Backend (Render) | https://lyftr-chatbot-aurv.onrender.com (`/health`) |
| Repo | https://github.com/TaranBytes/lyftr-chatbot |

> The backend runs on Render's free plan. The first request after a long idle can take 30–60 s while it wakes up. See [Known limitations](#known-limitations).

## 1. Features

- **Multi-turn chat.** The browser sends only the new message; history is loaded from SQLite per `session_id`.
- **Key failover.** If key_1 returns 429, a quota error or a 5xx, the same request silently retries on key_2 (then key_3, ...).
- **Response caching.** LiteLLM's built-in cache. The key is the model plus the *full* message list (system prompt + history + new message). A hit returns the identical reply and stored token counts, labelled `LLM_CACHE`.
- **Per-request metadata.** `resolution_source`, `model`, `latency_ms`, `input_tokens`, `output_tokens`, `key_id`, `fallback_attempts`. Tokens are never cumulative.
- **Failure simulation** for demos via the `X-Simulate-Failure` header (or a dropdown in the UI).
- **JSON-only errors.** 422 for bad input, 503 `{"detail":"all providers unavailable"}` when every key fails, 404 for an unknown session. No stack traces, no HTML error pages.

## 2. Architecture

```
Browser ──► React (Vite) ──► FastAPI :8000 ──► SQLite  /data/app.db
                                  │
                                  └─► LiteLLM Router + cache ──► Gemini key_1 ──(429/5xx)──► key_2
```

```
backend/app/   config.py  schemas.py  db.py  llm.py  main.py
backend/tests/ pytest suite (real LiteLLM against a local fake LLM server)
frontend/src/  App.jsx  api.js  styles.css  App.test.jsx
render.yaml    Render blueprint (backend)
frontend/vercel.json   Vercel config (frontend)
docker-compose.yml  Makefile  .env.example
```

How failover works: `llm.build_router` creates one LiteLLM model group per key (`chat-key_1`, `chat-key_2`, ...) with group-level fallbacks, `num_retries=0` and cooldowns disabled. A failed key is therefore never blacklisted, and the next request starts at key_1 again.

## 3. Run locally (Docker)

Requirements: Docker Desktop.

```bash
cp .env.example .env      # then put your real Gemini keys in LLM_API_KEYS
make up                   # builds and starts both containers
```

- Frontend: http://localhost:3000
- Backend: http://localhost:8000/health
- Stop: `make down` (chat history stays in the `chat-data` Docker volume)

### Environment variables

| Variable | Meaning | Default / example |
|---|---|---|
| `LLM_API_KEYS` | Comma-separated Gemini keys, at least 2. Ids are `key_1`, `key_2`, ... in order | required |
| `LLM_MODEL` | LiteLLM model name used for every key | `gemini/gemini-3.5-flash-lite` |
| `DATABASE_URL` | SQLite file | `sqlite:////data/app.db` |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins, no trailing slash | `http://localhost:3000` |
| `ENABLE_FAILURE_SIMULATION` | `true` enables the `X-Simulate-Failure` header | `true` |
| `VITE_API_URL` | Backend URL as seen by the browser (**build time**, frontend) | `http://localhost:8000` |
| `LLM_TIMEOUT_SECONDS`, `DEFAULT_SYSTEM_PROMPT` | Optional | `30`, a short helpful-assistant prompt |

**Never commit `.env`.** It is git-ignored; only `.env.example` (placeholders) is committed.

### Without Docker

```bash
cd backend && pip install -r requirements.txt
LLM_API_KEYS=k1,k2 uvicorn app.main:create_app_from_env --factory --port 8000
cd frontend && npm install && npm run dev      # http://localhost:3000
```

## 4. API

Exactly three endpoints (`/docs` and `/openapi.json` are disabled on purpose).

**`GET /health`** → `200 {"status":"ok","keys_configured":2,"cache_enabled":true}` (503 if the DB or keys are missing)

**`POST /chat`**
```json
{ "session_id": "s-7f3a", "message": "What is the capital of France?", "system_prompt": "optional" }
```
`session_id` 1–128 chars, `message` 1–4000 chars after trimming.
```json
{ "session_id": "s-7f3a", "reply": "Paris.",
  "metadata": { "resolution_source": "LIVE_INFERENCE", "model": "gemini/gemini-3.5-flash-lite",
                "latency_ms": 532, "input_tokens": 25, "output_tokens": 1,
                "key_id": "key_1", "fallback_attempts": 0 } }
```
On a cache hit: `resolution_source` is `LLM_CACHE`, `key_id` is `null`, `fallback_attempts` is `0`.
Errors: `422 {"detail": ...}`, `503 {"detail":"all providers unavailable"}`. Failed requests are never stored in history.

**`GET /sessions/{session_id}/messages`** → messages oldest-first (system prompt not included, metadata on assistant messages). Unknown id → `404 {"detail":"session not found"}`.

## 5. Demonstrating caching and failover

**Cache.** The key is the whole message list, so only an identical *conversation* hits. Send the same first message twice from two different sessions (reload the page between them): the second says `LLM_CACHE` in a few ms. Repeating the same text *later in the same conversation* is a miss, because the history is now part of the key.

**Failover from the UI.** Use the **Simulate failure** dropdown in the header (or open the page with `?simulate=key_1=429`):

| Option | Result |
|---|---|
| off | normal, `key_1 · 0 fallbacks` |
| key_1 fails (429) | normal reply, footer shows `key_2 · 1 fallbacks` |
| all keys fail | red error bubble (HTTP 503) |

**Failover from curl:**
```bash
curl -s -X POST https://lyftr-chatbot-aurv.onrender.com/chat \
  -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"demo1","message":"Name a colour."}'
```
Header forms: `key_1=429`, `key_1=429,key_2=500`, `*=429`; statuses 429/500/502/503. It only works when `ENABLE_FAILURE_SIMULATION=true`, affects only that one request, and requests carrying it skip the cache read so demos are deterministic.

## 6. Tests

```bash
cd backend && pip install -r requirements-dev.txt && pytest     # 58 tests
cd frontend && npm install && npm test                          # 16 tests
```

The backend tests do not mock LiteLLM: they start a local fake OpenAI-compatible server (real HTTP 429/500 responses) and point the real Router, cache and fallback code at it. They cover the contract, multi-turn history, cache hit/miss rules, per-request tokens, failover for 429/500/502/503, 503 when all keys fail, persistence across app restarts, validation, CORS and JSON-only errors.

Also verified manually: real Gemini through Docker, a container restart keeping history, and the deployed Render + Vercel pair.

## 7. Deployment

**Backend → Render** (Web Service, Docker): Root Directory `backend`, Instance Type Free, health check path `/health`. Env vars: `LLM_API_KEYS`, `LLM_MODEL`, `ENABLE_FAILURE_SIMULATION=true`, `DATABASE_URL=sqlite:////data/app.db`, and `ALLOWED_ORIGINS` set to the exact Vercel URL (no trailing slash). `render.yaml` is included as a blueprint.

**Frontend → Vercel:** Root Directory `frontend`, env var `VITE_API_URL` = the Render URL (no trailing slash). `VITE_API_URL` is baked in at build time, so changing it requires a redeploy.

**Keep-alive:** a free UptimeRobot HTTP monitor pings `/health` every 5 minutes so Render's free instance does not sleep. `/health` makes no LLM call, so it costs no quota.

## Known limitations

- **Render free has no persistent disk.** The SQLite file and the in-memory cache are wiped whenever the service restarts or redeploys (every code push, env var change, or occasional platform restart). Locally with Docker the volume persists and history survives `docker compose restart backend`. For durable history on Render use a paid instance with a disk mounted at `/data`.
- **The cache is in-memory** (allowed by the assignment) and is emptied on every restart. The backend runs a single worker so all requests share one cache.
- **Free-tier cold starts:** the first request after idle can take 30–60 s; the uptime monitor prevents this.
- **No auth or rate limiting.** Anyone with the backend URL can use the API and spend the Gemini quota. Rotate the keys after grading.
- LiteLLM is pinned to `1.77.7`; newer versions pull in proxy-only dependencies when the cache is used.
- The PDF's example model `gemini-2.0-flash` was shut down on 1 Jun 2026, so the model is set via `LLM_MODEL`.
