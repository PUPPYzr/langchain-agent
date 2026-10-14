"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for the model used by the weather agent."""

    openai_api_key: str
    openai_model: str
    openai_base_url: str | None
    model_temperature: float
    model_timeout_seconds: float
    model_max_retries: int
    model_max_completion_tokens: int
    agent_max_turns: int
    agent_runtime_type: str = "legacy"
    agent_checkpoint_db_path: str = "agent_runs.sqlite3"
    agent_run_timeout_seconds: float = 0.0

    @property
    def resolved_agent_run_timeout_seconds(self) -> float:
        """Return the whole-run deadline, expanding the model budget when unset.

        One Run can perform up to ``agent_max_turns`` model calls plus tool
        round trips, so budgeting a single model timeout for the entire run is
        too tight. When ``AGENT_RUN_TIMEOUT_SECONDS`` is unset (<=0), assume one
        model call per turn plus a final synthesis call.
        """
        if self.agent_run_timeout_seconds > 0:
            return self.agent_run_timeout_seconds
        return self.model_timeout_seconds * (self.agent_max_turns + 1)

    @classmethod
    def from_environment(cls) -> "Settings":
        """Load and validate settings from the process environment."""
        load_dotenv()

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("OPENAI_MODEL", "").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        temperature_value = os.getenv("MODEL_TEMPERATURE", "0").strip()
        model_timeout_value = os.getenv("MODEL_TIMEOUT_SECONDS", "30").strip()
        model_retries_value = os.getenv("MODEL_MAX_RETRIES", "0").strip()
        model_tokens_value = os.getenv("MODEL_MAX_COMPLETION_TOKENS", "400").strip()
        agent_turns_value = os.getenv("AGENT_MAX_TURNS", "4").strip()
        run_timeout_value = os.getenv("AGENT_RUN_TIMEOUT_SECONDS", "0").strip()
        runtime_type = os.getenv("AGENT_RUNTIME_TYPE", "legacy").strip().lower()
        checkpoint_db_path = checkpoint_db_path_from_environment()

        missing = [
            name
            for name, value in (
                ("OPENAI_API_KEY", api_key),
                ("OPENAI_MODEL", model),
            )
            if not value
        ]
        if missing:
            missing_names = ", ".join(missing)
            raise ConfigurationError(
                f"Missing required environment variables: {missing_names}. "
                "Create a .env file based on .env.example."
            )

        try:
            temperature = float(temperature_value)
            model_timeout = float(model_timeout_value)
            model_retries = int(model_retries_value)
            model_max_completion_tokens = int(model_tokens_value)
            agent_max_turns = int(agent_turns_value)
            run_timeout = float(run_timeout_value)
        except ValueError as exc:
            raise ConfigurationError(
                "MODEL_TEMPERATURE, MODEL_TIMEOUT_SECONDS, "
                "MODEL_MAX_RETRIES, MODEL_MAX_COMPLETION_TOKENS, "
                "AGENT_MAX_TURNS and AGENT_RUN_TIMEOUT_SECONDS must use valid "
                "numeric values."
            ) from exc

        if model_timeout <= 0:
            raise ConfigurationError("MODEL_TIMEOUT_SECONDS must be greater than 0.")
        if model_retries < 0:
            raise ConfigurationError("MODEL_MAX_RETRIES must not be negative.")
        if model_max_completion_tokens <= 0:
            raise ConfigurationError(
                "MODEL_MAX_COMPLETION_TOKENS must be greater than 0."
            )
        if agent_max_turns < 1:
            raise ConfigurationError("AGENT_MAX_TURNS must be at least 1.")
        if agent_max_turns > 20:
            raise ConfigurationError("AGENT_MAX_TURNS must not be greater than 20.")
        if run_timeout < 0:
            raise ConfigurationError(
                "AGENT_RUN_TIMEOUT_SECONDS must not be negative."
            )
        if run_timeout > 0 and run_timeout < model_timeout:
            raise ConfigurationError(
                "AGENT_RUN_TIMEOUT_SECONDS must be at least MODEL_TIMEOUT_SECONDS."
            )
        if runtime_type not in {"legacy", "langgraph"}:
            raise ConfigurationError(
                "AGENT_RUNTIME_TYPE must be either 'legacy' or 'langgraph'."
            )

        return cls(
            openai_api_key=api_key,
            openai_model=model,
            openai_base_url=base_url,
            model_temperature=temperature,
            model_timeout_seconds=model_timeout,
            model_max_retries=model_retries,
            model_max_completion_tokens=model_max_completion_tokens,
            agent_max_turns=agent_max_turns,
            agent_runtime_type=runtime_type,
            agent_checkpoint_db_path=checkpoint_db_path,
            agent_run_timeout_seconds=run_timeout,
        )


def checkpoint_db_path_from_environment() -> str:
    """Resolve the SQLite path without requiring full model configuration.

    Management commands (Run/Thread/Checkpoint queries) only need the database
    location, so they must not fail when model credentials are absent.
    """
    load_dotenv()
    configured = os.getenv("AGENT_CHECKPOINT_DB_PATH", "").strip()
    return configured or "agent_runs.sqlite3"
