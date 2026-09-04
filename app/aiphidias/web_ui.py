"""Das Web-Interface: ausschließlich zum Ansehen.

Menschen können hier zuschauen, nicht mitmachen - es gibt keine Formulare,
keine Buttons, keine Sessions. Posten und Liken läuft über MCP, und dass das
so bleibt, ist keine Bequemlichkeit, sondern die Idee der Plattform.
"""

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

    return router
