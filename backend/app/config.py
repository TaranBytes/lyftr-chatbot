"""Environment-driven configuration.

Everything the backend needs comes from environment variables so the same image
runs locally (docker compose) and in production (Railway) without code changes.
"""
import os
from dataclasses import dataclass, field

# LiteLLM tries to download its model price list from GitHub on import. Use the
# bundled copy instead so start-up never depends on the network.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, concise assistant. Answer clearly and keep earlier "
    "messages in this conversation in mind."
)


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Key pool. Order matters: first entry is key_1, second key_2, ...
    api_keys: list[str] = field(default_factory=list)
    # LiteLLM model name, e.g. "gemini/gemini-3.5-flash-lite".
    llm_model: str = "gemini/gemini-3.5-flash-lite"
    database_url: str = "sqlite:///./data/app.db"
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])
    enable_failure_simulation: bool = False
    default_system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Optional override of the provider URL. Not needed for Gemini; used by the
    # test-suite to point LiteLLM at a local fake LLM server.
    llm_api_base: str | None = None
    llm_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        origins = [o.rstrip("/") for o in _split_csv(os.getenv("ALLOWED_ORIGINS"))]
        return cls(
            api_keys=_split_csv(os.getenv("LLM_API_KEYS")),
            llm_model=os.getenv("LLM_MODEL", cls.llm_model),
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            allowed_origins=origins or ["http://localhost:3000"],
            enable_failure_simulation=_truthy(os.getenv("ENABLE_FAILURE_SIMULATION")),
            default_system_prompt=os.getenv("DEFAULT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            llm_api_base=os.getenv("LLM_API_BASE") or None,
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        )

    @property
    def sqlite_path(self) -> str:
        """'sqlite:////data/app.db' -> '/data/app.db'; 'sqlite:///./x.db' -> './x.db'."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("DATABASE_URL must start with 'sqlite:///' (SQLite only)")
        return self.database_url[len(prefix):]
