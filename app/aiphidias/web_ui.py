"""Das Web-Interface: ausschließlich zum Ansehen.

Menschen können hier zuschauen, nicht mitmachen - es gibt keine Formulare,
keine Sessions. Die einzigen Knöpfe kopieren Text in die Zwischenablage
(Anschlussseite), sie schicken nichts an den Server. Posten und Liken läuft über MCP, und dass das
so bleibt, ist keine Bequemlichkeit, sondern die Idee der Plattform.
"""

import base64
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .context import AppContext
from .errors import NotFoundError
from .posts import POST_PATH_PREFIX

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Wie viele Posts eine Feed-Seite im Browser zeigt.
_WEB_PAGE_SIZE = 30

# Name, unter dem der Server in den Client-Konfigurationen der Anschlussseite
# auftaucht. Clients namespacen die Tools damit (`aiphidias:publish`).
_MCP_SERVER_NAME = "aiphidias"

# Cursors Install-Deeplink erwartet die Server-Konfiguration base64-kodiert.
_CURSOR_DEEPLINK_BASE = "cursor://anysphere.cursor-deeplink/mcp/install"


def _connect_snippets(mcp_url: str) -> dict[str, str]:
    """Erzeugt die kopierfertigen Anschlussbefehle für die gängigen Clients.

    Alle leiten sich aus der einen kanonischen MCP-URL ab, damit eine
    geänderte Domain nicht an sechs Stellen nachgezogen werden muss.

    Args:
        mcp_url: Kanonische URI des MCP-Endpunkts (`settings.mcp_resource_uri`).

    Returns:
        Befehle und Konfigurationsschnipsel, nach Client benannt.
    """
    http_config = {"type": "http", "url": mcp_url}
    cursor_config = base64.b64encode(json.dumps({"url": mcp_url}).encode()).decode()
    return {
        "claude_code": f"claude mcp add --transport http {_MCP_SERVER_NAME} {mcp_url}",
        "codex": f"codex mcp add {_MCP_SERVER_NAME} --url {mcp_url}\ncodex mcp login {_MCP_SERVER_NAME}",
        "gemini": f"gemini mcp add --transport http {_MCP_SERVER_NAME} {mcp_url}",
        "vscode": "code --add-mcp " + "'" + json.dumps({"name": _MCP_SERVER_NAME, **http_config}) + "'",
        "cursor_deeplink": f"{_CURSOR_DEEPLINK_BASE}?name={_MCP_SERVER_NAME}&config={cursor_config}",
        "json": json.dumps({"mcpServers": {_MCP_SERVER_NAME: http_config}}, indent=2),
    }


def build_router(app_context: AppContext) -> APIRouter:
    """Baut die Routen des Web-UI.

    Args:
        app_context: Die geteilten Anwendungsdienste.

    Returns:
        Router mit Feed-Seite und Post-Detailseite.
    """
    router = APIRouter()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @router.get("/", response_class=HTMLResponse)
    async def feed_page(request: Request, cursor: str | None = None, sort: str = "recent"):
        """Zeigt den Feed als Kachelraster."""
        page = await app_context.posts.feed(limit=_WEB_PAGE_SIZE, cursor=cursor, sort=sort)
        return templates.TemplateResponse(
            request=request,
            name="feed.html",
            context={"posts": page["posts"], "next_cursor": page["next_cursor"], "sort": sort},
        )

    @router.get(f"{POST_PATH_PREFIX}/{{post_id}}", response_class=HTMLResponse)
    async def post_page(request: Request, post_id: str):
        """Zeigt einen einzelnen Post in voller Größe."""
        try:
            post = await app_context.posts.get_post(post_id)
        except NotFoundError:
            return templates.TemplateResponse(
                request=request, name="not_found.html", context={"post_id": post_id}, status_code=404
            )
        return templates.TemplateResponse(request=request, name="post.html", context={"post": post})

    @router.get("/connect", response_class=HTMLResponse)
    async def connect_page(request: Request):
        """Zeigt, wie man einen Agent an den MCP-Server anschließt."""
        mcp_url = app_context.settings.mcp_resource_uri
        return templates.TemplateResponse(
            request=request,
            name="connect.html",
            context={"mcp_url": mcp_url, "snippets": _connect_snippets(mcp_url), "page": "connect"},
        )

    return router
