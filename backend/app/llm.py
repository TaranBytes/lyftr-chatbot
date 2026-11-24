"""LiteLLM integration: key pool, automatic failover, response cache, failure simulation.

How the three LiteLLM features are wired
  * Key pool   -> one LiteLLM Router "model group" per API key: chat-key_1, chat-key_2, ...
                  (each deployment carries model_info.id = "key_1", "key_2", ...)
  * Failover   -> Router(fallbacks=[{"chat-key_1": ["chat-key_2", ...]}]). We always call
                  chat-key_1; if it raises (429, quota, 5xx, timeout...) LiteLLM retries the
                  same request on the next group. Cooldowns are disabled so a failed key is
                  tried again on the very next request (and simulated failures never "stick").
  * Cache      -> LiteLLM's built-in in-memory cache (litellm.Cache(type="local")), switched on
                  per request with caching=True. LiteLLM builds the key from model + messages.
"""
import time
from contextvars import ContextVar
from dataclasses import dataclass

import litellm
from litellm import Router
from litellm.integrations.custom_logger import CustomLogger

from .config import Settings

SIMULATED_STATUSES = {429, 500, 502, 503}
FIRST_GROUP = "chat-key_1"

# Failures requested for the *current request only* (set from the X-Simulate-Failure header).
# A ContextVar is isolated per asyncio task, so concurrent requests can't affect each other.
_simulated_failures: ContextVar[dict[str, int]] = ContextVar("simulated_failures", default={})


class AllProvidersUnavailable(Exception):
    """Every key in the pool failed (or the provider returned nothing usable)."""


@dataclass
class LLMResult:
    reply: str
    resolution_source: str  # "LIVE_INFERENCE" | "LLM_CACHE"
    model: str
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int


def parse_simulation_header(value: str | None) -> dict[str, int]:
    """'key_1=429,key_2=500' -> {'key_1': 429, 'key_2': 500}; '*=429' -> {'*': 429}.

    Malformed or unsupported pairs are ignored rather than rejected: this is a test hook.
    """
    result: dict[str, int] = {}
    for pair in (value or "").split(","):
        key, sep, status = pair.partition("=")
        key, status = key.strip(), status.strip()
        if sep and key and status.isdigit() and int(status) in SIMULATED_STATUSES:
            result[key] = int(status)
    return result


def _simulated_exception(status: int) -> Exception:
    """Build the exception LiteLLM would raise if the provider answered with `status`."""
    kwargs = {"message": f"simulated provider error {status}", "model": "simulated", "llm_provider": "simulated"}
    if status == 429:
        return litellm.RateLimitError(**kwargs)
    if status == 500:
        return litellm.InternalServerError(**kwargs)
    if status == 503:
        return litellm.ServiceUnavailableError(**kwargs)
    return litellm.APIError(status_code=status, **kwargs)  # 502


class FailureSimulationHook(CustomLogger):
    """Runs right after LiteLLM picked a deployment and right before it calls the provider.

    If this deployment's key id is listed in the current request's simulation map we make
    LiteLLM raise the matching provider error. From the Router's point of view this is a real
    provider failure, so the real fallback code path is exercised.
    """

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        failures = _simulated_failures.get()
        if not failures:
            return kwargs
        key_id = ((kwargs.get("metadata") or {}).get("model_info") or {}).get("id")
        status = failures.get(key_id, failures.get("*"))
        if status:
            kwargs["mock_response"] = _simulated_exception(status)
        return kwargs


def build_router(settings: Settings) -> Router:
    model_list = []
    for index, api_key in enumerate(settings.api_keys, start=1):
        params = {"model": settings.llm_model, "api_key": api_key}
        if settings.llm_api_base:
            params["api_base"] = settings.llm_api_base
        model_list.append(
            {
                "model_name": f"chat-key_{index}",
                "litellm_params": params,
                "model_info": {"id": f"key_{index}"},
            }
        )
    later_groups = [f"chat-key_{i}" for i in range(2, len(settings.api_keys) + 1)]
    return Router(
        model_list=model_list,
        fallbacks=[{FIRST_GROUP: later_groups}] if later_groups else [],
        num_retries=0,  # no blind retries on the same key; fail over instead
        disable_cooldowns=True,
        timeout=settings.llm_timeout_seconds,
        cache_responses=True,
        cache_kwargs={"type": "local"},
    )


class LLMService:
    def __init__(self, settings: Settings):
        self.settings = settings
        litellm.suppress_debug_info = True
        litellm.callbacks = [cb for cb in litellm.callbacks if not isinstance(cb, FailureSimulationHook)]
        litellm.callbacks.append(FailureSimulationHook())
        self.router = build_router(settings) if settings.api_keys else None

    @property
    def keys_configured(self) -> int:
        return len(self.settings.api_keys)

    def _describe(self, exc: Exception) -> str:
        """One log-safe line about a provider failure: error type, HTTP status, short message.
        Server log only (never sent to the client). API keys are masked."""
        text = " ".join(str(exc).split())
        for key in self.settings.api_keys:
            text = text.replace(key, "***")
        return f"{type(exc).__name__} status={getattr(exc, 'status_code', None)} {text[:300]}"

    async def complete(self, messages: list[dict], simulate: dict[str, int] | None = None) -> LLMResult:
        """Send `messages` through the key pool. Raises AllProvidersUnavailable on total failure."""
        if self.router is None:
            raise AllProvidersUnavailable("no API keys configured")

        request_options: dict = {"caching": True}
        if simulate:
            # A simulated-failure request must really hit the (simulated) providers, so it
            # never reads from the cache. That keeps failover/503 demos deterministic.
            request_options["cache"] = {"no-cache": True}

        token = _simulated_failures.set(simulate or {})
        try:
            response = await self.router.acompletion(model=FIRST_GROUP, messages=messages, **request_options)
        except Exception as exc:  # LiteLLM raises many exception types; all mean "no key worked"
            raise AllProvidersUnavailable(self._describe(exc)) from exc
        finally:
            _simulated_failures.reset(token)

        reply = (response.choices[0].message.content or "").strip()
        if not reply:
            raise AllProvidersUnavailable("empty reply from provider")

        hidden = response._hidden_params or {}
        cache_hit = bool(hidden.get("cache_hit"))
        usage = response.usage
        if cache_hit:
            key_id, fallbacks = None, 0
        else:
            key_id = hidden.get("model_id")
            fallbacks = int((hidden.get("additional_headers") or {}).get("x-litellm-attempted-fallbacks") or 0)
        return LLMResult(
            reply=reply,
            resolution_source="LLM_CACHE" if cache_hit else "LIVE_INFERENCE",
            model=self.settings.llm_model,
            input_tokens=int(usage.prompt_tokens or 0),
            output_tokens=int(usage.completion_tokens or 0),
            key_id=key_id,
            fallback_attempts=fallbacks,
        )


def now_ms() -> float:
    return time.perf_counter() * 1000
