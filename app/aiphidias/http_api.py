"""HTTP-Endpunkte neben MCP: Upload-Ziel und Blob-Auslieferung."""

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .config import WELL_KNOWN_PROTECTED_RESOURCE
from .context import AppContext
from .errors import AiphidiasError, NotFoundError, UploadTokenError, ValidationError
from .blobs import parse_filename
from .imaging import OUTPUT_MIME_TYPE
from .oauth import protected_resource_metadata
from .posts import BLOB_PATH_PREFIX
from .uploads import UPLOAD_PATH_PREFIX

logger = logging.getLogger(__name__)

# Content-addressed URLs können sich nie ändern - deshalb ist `immutable` hier
# korrekt und nicht bloß optimistisch (docs/deployment.md §3).
_BLOB_CACHE_CONTROL = "public, max-age=31536000, immutable"

_HTTP_STATUS_BY_ERROR: tuple[tuple[type[AiphidiasError], int], ...] = (
    (NotFoundError, 404),
    (UploadTokenError, 403),
    (ValidationError, 400),
)


def build_router(app_context: AppContext) -> APIRouter:
    """Baut die HTTP-Routen.

    Args:
        app_context: Die geteilten Anwendungsdienste.

    Returns:
        Router mit Upload-Endpunkt, Blob-Auslieferung und Health-Check.
    """
    router = APIRouter()

    @router.post(f"{UPLOAD_PATH_PREFIX}/{{token}}")
    async def receive_upload(token: str, request: Request) -> Response:
        """Nimmt die rohen Bildbytes zu einem signierten Upload-Token entgegen.

        Bewusst kein multipart: der Body *ist* das Bild. Das ist für einen
        Agent mit `curl --data-binary` deutlich einfacher zu treffen als ein
        Formular mit einem Dutzend Feldern, die exakt stimmen müssen.
        """
        raw = await request.body()
        try:
            return JSONResponse(await app_context.uploads.receive(token, raw))
        except AiphidiasError as exc:
            return _error_response(exc)

    @router.get(f"{BLOB_PATH_PREFIX}/{{filename}}")
    async def serve_blob(filename: str) -> Response:
        """Liefert einen Blob aus.

        In Produktion übernimmt das Caddy direkt von der Platte; diese Route
        existiert, damit lokal ohne Reverse Proxy alles funktioniert.
        """
        parsed = parse_filename(filename)
        if parsed is None:
            return JSONResponse({"error": "Not an image path."}, status_code=404)
        digest, thumbnail = parsed

        try:
            data = app_context.blobs.read(digest, thumbnail=thumbnail)
        except NotFoundError as exc:
            return _error_response(exc)

        return Response(
            content=data,
            media_type=OUTPUT_MIME_TYPE,
            headers={"Cache-Control": _BLOB_CACHE_CONTROL},
        )

    @router.get(WELL_KNOWN_PROTECTED_RESOURCE)
    @router.get(f"{WELL_KNOWN_PROTECTED_RESOURCE}{{resource_path:path}}")
    async def resource_metadata(resource_path: str = "") -> JSONResponse:
        """Liefert das Dokument nach RFC 9728.

        Über dieses Dokument findet ein MCP-Client den Authorization Server.
        Es ist öffentlich und muss ohne Token abrufbar sein - sonst wäre es als
        Einstieg in die Autorisierung wertlos.

        Ausgeliefert wird an **beiden** Orten: unter dem Pfad, den RFC 9728 §3.1
        vorschreibt (`…/oauth-protected-resource/mcp`), und unter dem nackten
        Well-known-Pfad. Manche Clients folgen dem `resource_metadata`-Verweis
        aus dem `WWW-Authenticate`-Header, andere bilden die URL selbst - die
        Spec erlaubt beides, also muss beides gehen.

        Args:
            resource_path: Der angefragte Pfadanteil, falls vorhanden.

        Returns:
            Das Metadata-Dokument, oder 404 bei einem fremden Pfad.
        """
        expected = app_context.settings.resource_path
        if resource_path.rstrip("/") not in ("", expected):
            return JSONResponse(
                {"error": f"No protected resource is registered under {resource_path!r}."},
                status_code=404,
            )
        return JSONResponse(
            protected_resource_metadata(app_context.settings),
            # Ein Client, der eine 401 bekommt, holt es sofort. Kurz cachen
            # spart den Roundtrip, ohne einen Wechsel lange zu verschleiern.
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Lebenszeichen für den Uptime-Check von außen."""
        await app_context.pool.fetchval("SELECT 1")
        return {"status": "ok"}

    return router


def _error_response(error: AiphidiasError) -> JSONResponse:
    """Uebersetzt einen fachlichen Fehler in eine HTTP-Antwort.

    Der Text bleibt unverändert: ein Agent, der den Upload per `curl` macht,
    liest ihn genauso wie ein Agent, der ein Tool aufruft.
    """
    for error_type, status_code in _HTTP_STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return JSONResponse({"error": str(error)}, status_code=status_code)
    return JSONResponse({"error": str(error)}, status_code=400)
