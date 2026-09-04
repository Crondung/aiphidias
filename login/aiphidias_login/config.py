"""Konfiguration der Login- und Consent-App."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Laufzeitkonfiguration, vollständig aus der Umgebung."""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    # --- Hydra -------------------------------------------------------------
    hydra_admin_url: str = "http://hydra:4445"
    # Öffentlicher Issuer. Muss exakt dem entsprechen, was Hydra ausliefert -
    # der Resource Server vergleicht ihn zeichenweise.
    hydra_public_url: str = "http://localhost:4444"

    # --- GitHub als Identity Provider -------------------------------------
    github_client_id: str = ""
    github_client_secret: str = ""
    github_callback_url: str = "http://localhost:3000/auth/github/callback"

    # --- Datenbank ---------------------------------------------------------
    database_url: str = "postgresql://aiphidias:aiphidias@postgres:5432/aiphidias"

    # --- Sicherheit --------------------------------------------------------
    # Resource-URIs, für die diese Instanz Tokens ausstellen darf. Alles andere
    # wird abgelehnt: ein Client soll sich keine fremde Audience holen können.
    allowed_resources: str = "http://localhost:8787/mcp"

    # Wie lange ein angefangener GitHub-Login gültig bleibt.
    login_state_ttl_seconds: int = 600

    @property
    def allowed_resource_set(self) -> frozenset[str]:
        """Die erlaubten Resource-URIs als Menge."""
        return frozenset(part.strip() for part in self.allowed_resources.split(",") if part.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Liefert die prozessweit einmalige Konfiguration."""
    return Settings()
