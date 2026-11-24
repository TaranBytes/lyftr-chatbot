"""FastAPI application: exactly three endpoints (/health, /chat, /sessions/{id}/messages)."""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database
from .llm import AllProvidersUnavailable, LLMService, parse_simulation_header
from .schemas import ChatRequest, ChatResponse

logger = logging.getLogger("lyftr.chat")


def create_app(settings: Settings) -> FastAPI:
    db = Database(settings.sqlite_path)
    llm = LLMService(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init_schema()  # schema is created automatically on every start-up
        if len(settings.api_keys) < 2:
            logger.warning("Only %d API key(s) configured; failover needs at least 2", len(settings.api_keys))
        yield

    # Docs/OpenAPI routes are switched off: the assignment says the backend exposes exactly three endpoints.
    app = FastAPI(title="Lyftr chatbot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def unhandled_error(_: Request, exc: Exception):
        # Never leak stack traces or provider details to the client; the details go to the server log only.
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/health")
    async def health():
        if not db.ping():
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        if llm.keys_configured == 0:
            return JSONResponse(status_code=503, content={"detail": "no api keys configured"})
        return {"status": "ok", "keys_configured": llm.keys_configured, "cache_enabled": True}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request):
        started = time.perf_counter()

        # 1. Build the full message list: system prompt + stored history + the new message.
        stored_prompt = db.get_session_prompt(body.session_id)
        requested_prompt = (body.system_prompt or "").strip() or None
        system_prompt = requested_prompt or stored_prompt or settings.default_system_prompt
        history = db.get_history(body.session_id)
        messages = [{"role": "system", "content": system_prompt}, *history, {"role": "user", "content": body.message}]

        # 2. Failure simulation is honoured only when explicitly enabled on the server.
        simulate = None
        if settings.enable_failure_simulation:
            simulate = parse_simulation_header(request.headers.get("x-simulate-failure")) or None

        # 3. Ask LiteLLM (cache -> key_1 -> key_2 ...). On total failure -> 503 and nothing is stored.
        try:
            result = await llm.complete(messages, simulate)
        except AllProvidersUnavailable as exc:
            logger.warning("All providers failed for session %s: %s", body.session_id, exc)
            return JSONResponse(status_code=503, content={"detail": "all providers unavailable"})

        # 4. Server-measured latency for this request (includes the cache lookup on a hit).
        latency_ms = int((time.perf_counter() - started) * 1000)
        metadata = {
            "resolution_source": result.resolution_source,
            "model": result.model,
            "latency_ms": latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "key_id": result.key_id,
            "fallback_attempts": result.fallback_attempts,
        }

        # 5. Success only: persist user message + assistant reply (with metadata) atomically.
        db.save_exchange(body.session_id, body.message, result.reply, metadata, requested_prompt)
        return {"session_id": body.session_id, "reply": result.reply, "metadata": metadata}

    @app.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: str):
        if not db.session_exists(session_id):
            return JSONResponse(status_code=404, content={"detail": "session not found"})
        return {"session_id": session_id, "messages": db.get_messages(session_id)}

    return app


def create_app_from_env() -> FastAPI:
    """Entry point for production: `uvicorn app.main:create_app_from_env --factory`."""
    logging.basicConfig(level=logging.INFO)
    # LiteLLM logs a full traceback for every failed-over attempt (which is normal operation here).
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    return create_app(Settings.from_env())
