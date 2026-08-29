"""Application settings.

Every tunable lives here and is driven by the environment. The one hard rule:
the process refuses to start with the placeholder SECRET_KEY, so the app fails
loudly rather than running insecurely.
"""

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET = "CHANGE_ME"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- database -------------------------------------------------------
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/money"
    test_database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/money_test"

    # --- security -------------------------------------------------------
    secret_key: str = PLACEHOLDER_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    bcrypt_rounds: int = 12

    # --- product --------------------------------------------------------
    signup_grant: Decimal = Decimal("100000.00")
    currency: str = "BDT"
    request_expiry_hours: int = 72

    # --- ops ------------------------------------------------------------
    environment: str = "development"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    def assert_bootable(self) -> None:
        """Called on application startup. Never skipped, never warned-and-continued."""
        if self.secret_key == PLACEHOLDER_SECRET or not self.secret_key.strip():
            raise RuntimeError(
                "SECRET_KEY is still the placeholder value.\n"
                "Generate one and put it in .env:\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters.")
        if self.signup_grant <= 0:
            raise RuntimeError("SIGNUP_GRANT must be positive.")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
