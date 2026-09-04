"""Einstiegspunkt: eine ASGI-Anwendung, drei Oberflächen.

Unter demselben Prozess laufen der MCP-Server (Streamable HTTP unter `/mcp`),
die HTTP-Endpunkte für Upload und Blobs und das lesende Web-UI. Ein Prozess,
weil sie sich Datenbankpool und Blob-Store teilen und getrennte Deployments
hier nichts kaufen würden außer Betriebsaufwand.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import asyncpg
from fastapi import FastAPI

from . import http_api, web_ui
from .auth_middleware import RequireAccessToken
from .config import Settings, get_settings
from .context import build_context
from .db import apply_migrations, create_pool
from .mcp_server import build_mcp_server
from .oauth import TokenVerifier

from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Wie lange beim Start auf Postgres gewartet wird. Beim `docker compose up`
# startet die Datenbank parallel und ist ein paar Sekunden lang nicht da.
_DB_CONNECT_ATTEMPTS = 30
_DB_CONNECT_DELAY_SECONDS = 1.0

MCP_PATH = "/mcp"


class AcceptBothSlashForms:
    """Reicht einen Aufruf an den MCP-Transport durch, egal wie der Pfad endet.

    Ein `Mount("/mcp")` allein beantwortet nur `/mcp/…`; auf das nackte `/mcp`
    antwortet Starlette mit `307` auf die Fassung mit Schrägstrich. Das ist aus
    zwei Gründen schädlich: hinter einem Reverse Proxy kennt die App das äußere
    Schema nicht und baut die Weiterleitung auf `http://`, und viele
    HTTP-Clients wiederholen ein `POST` nach einer Weiterleitung nicht.

    Für einen Endpunkt, dessen Adresse in fremden Konfigurationen steht, ist
    Umleiten die falsche Antwort. Deshalb wird der Transport zusätzlich als
    eigene Route für den exakten Pfad registriert und dieser Shim
    vereinheitlicht, was innen ankommt.
    """

    def __init__(self, app: object) -> None:
        """Args:
        app: Die eingehängte Anwendung, deren Route auf `/` liegt.
        """
        self._app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        """Vereinheitlicht den Pfad innerhalb des Mounts und delegiert."""
        if scope["type"] == "http":
            scope = {**scope, "path": "/"}
        await self._app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Baut die ASGI-Anwendung.

    Args:
        settings: Konfiguration; None nimmt die aus der Umgebung.

    Returns:
        Die fertig verdrahtete FastAPI-Anwendung.
    """
    effective_settings = settings or get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    # Wird im Lifespan befüllt; die Routen greifen über `app.state` darauf zu.
    app = FastAPI(title="AIPHIDIAS", version="0.1.0", lifespan=_lifespan_factory(effective_settings))
    return app


def _lifespan_factory(settings: Settings):
    """Erzeugt den Lifespan, der Pool, Migrationen und MCP-Session-Manager hält.

    Der MCP-Transport bringt einen eigenen Lifespan mit. Ein per `mount`
    eingehängtes Starlette-App bekommt den aber nicht gestartet - deshalb wird
    der Session-Manager hier explizit mitgeführt.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.blob_root.mkdir(parents=True, exist_ok=True)

        pool = await _connect_with_retry(settings.database_url)
        applied = await apply_migrations(pool)
        logger.info("Migrationen: %s", ", ".join(applied) if applied else "keine neuen")

        app_context = build_context(settings, pool)
        app.state.context = app_context

        mcp = build_mcp_server(app_context)
        # `streamable_http_path="/"`, weil der Mount schon `/mcp` vorgibt -
        # sonst läge der Endpunkt unter `/mcp/mcp`.
        # Der DNS-Rebinding-Schutz des SDK lässt standardmäßig nur localhost
        # als Host-Header zu. Hinter einem Reverse Proxy steht dort der
        # öffentliche Name - der muss ausdrücklich erlaubt werden, sonst
        # weist der Transport jeden Aufruf ab, noch bevor ein Tool ihn sieht.
        transport = mcp.streamable_http_app(
            streamable_http_path="/",
            transport_security=TransportSecuritySettings(
                allowed_hosts=_allowed_hosts(settings.mcp_base_url),
                allowed_origins=[settings.mcp_base_url],
            ),
        )
        if not settings.dev_mode:
            # Ohne Token endet der Aufruf hier mit 401 - erst das bringt einen
            # MCP-Client dazu, den Autorisierungsablauf zu starten.
            transport = RequireAccessToken(transport, settings, TokenVerifier(settings))
        endpoint = AcceptBothSlashForms(transport)
        # Der Mount bedient `/mcp/`, die Route den exakten Pfad `/mcp`.
        app.mount(MCP_PATH, endpoint)
        app.router.routes.append(
            Route(MCP_PATH, endpoint=endpoint, methods=["GET", "POST", "DELETE", "OPTIONS"])
        )
        app.include_router(http_api.build_router(app_context))
        app.include_router(web_ui.build_router(app_context))

        logger.info("AIPHIDIAS bereit: MCP unter %s, Web-UI unter /", MCP_PATH)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await pool.close()

    return lifespan


def _allowed_hosts(base_url: str) -> list[str]:
    """Ermittelt die Host-Header, die der MCP-Transport akzeptieren darf.

    Args:
        base_url: Die öffentliche Basis-URL dieses MCP-Servers.

    Returns:
        Der Hostname mit und ohne Port - je nachdem, was der Proxy durchreicht.
    """
    parsed = urlparse(base_url)
    hosts = [parsed.netloc]
    if parsed.hostname and parsed.hostname != parsed.netloc:
        hosts.append(parsed.hostname)
    return hosts


async def _connect_with_retry(database_url: str) -> asyncpg.Pool:
    """Baut den Pool auf und wartet dabei auf eine startende Datenbank.

    Args:
        database_url: libpq-URL der Zieldatenbank.

    Returns:
        Der offene Pool.

    Raises:
        OSError: Datenbank auch nach allen Versuchen nicht erreichbar.
    """
    last_error: Exception | None = None
    for attempt in range(1, _DB_CONNECT_ATTEMPTS + 1):
        try:
            return await create_pool(database_url)
        except (OSError, asyncpg.PostgresError) as exc:
            last_error = exc
            logger.info("Warte auf Postgres (Versuch %d/%d)", attempt, _DB_CONNECT_ATTEMPTS)
            await asyncio.sleep(_DB_CONNECT_DELAY_SECONDS)

    raise OSError(f"Postgres nicht erreichbar: {last_error}")


app = create_app()
