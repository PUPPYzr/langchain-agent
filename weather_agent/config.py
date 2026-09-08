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

    @classmethod
    def from_environment(cls) -> "Settings":
        """Load and validate settings from the process environment."""
        load_dotenv()

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("OPENAI_MODEL", "").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        temperature_value = os.getenv("MODEL_TEMPERATURE", "0").strip()

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
        except ValueError as exc:
            raise ConfigurationError("MODEL_TEMPERATURE must be a number.") from exc

        return cls(
            openai_api_key=api_key,
            openai_model=model,
            openai_base_url=base_url,
            model_temperature=temperature,
        )
