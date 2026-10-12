"""Zusammenbau der Anwendungsdienste.

Ein Ort, an dem Pool, Blob-Store, Identität und die beiden Services
zusammenkommen - damit MCP-Tools, HTTP-Endpunkte und Web-UI dieselben Instanzen
benutzen und keiner sich seine eigenen baut.
"""

from dataclasses import dataclass

import asyncpg

from .blobs import BlobStore
from .config import Settings
from .identity import DevIdentityProvider, IdentityProvider, OAuthIdentityProvider
from .oauth import TokenVerifier
from .posts import PostService
from .rendering import Renderer
from .uploads import UploadService


@dataclass(frozen=True, slots=True)
class AppContext:
    """Die zur Laufzeit geteilten Bausteine der Anwendung."""

    settings: Settings
    pool: asyncpg.Pool
    blobs: BlobStore
    identity: IdentityProvider
    uploads: UploadService
    renderer: Renderer
    posts: PostService


def build_context(settings: Settings, pool: asyncpg.Pool) -> AppContext:
    """Verdrahtet die Dienste.

    Args:
        settings: Laufzeitkonfiguration.
        pool: Offener Verbindungspool.

    Returns:
        Der fertige Anwendungskontext.

    Raises:
        RuntimeError: `dev_mode` ist an, obwohl der Dienst nicht auf localhost
            läuft - der Header-Provider authentifiziert nichts und darf
            niemals öffentlich erreichbar sein.
    """
    if settings.dev_mode and not _is_local(settings.mcp_base_url):
        raise RuntimeError(
            f"dev_mode=true, aber mcp_base_url ist {settings.mcp_base_url!r}. Der "
            "Dev-Identitätsprovider liest die Identität aus einem frei wählbaren Header "
            "und darf nicht öffentlich erreichbar sein. Entweder dev_mode abschalten "
            "oder lokal betreiben."
        )

    blobs = BlobStore(root=settings.blob_root)
    uploads = UploadService(pool, blobs, settings)
    # Prozessweit einer: der Renderer trägt die Semaphore, die dafür sorgt,
    # dass nie zwei Läufe gleichzeitig um dieselben Kerne konkurrieren.
    renderer = Renderer(settings)
    posts = PostService(pool, blobs, uploads, renderer, settings)

    return AppContext(
        settings=settings,
        pool=pool,
        blobs=blobs,
        identity=(
            DevIdentityProvider(settings.dev_account_handle)
            if settings.dev_mode
            else OAuthIdentityProvider(TokenVerifier(settings))
        ),
        uploads=uploads,
        renderer=renderer,
        posts=posts,
    )


def _is_local(base_url: str) -> bool:
    """Sagt, ob eine Basis-URL auf die eigene Maschine zeigt."""
    return any(host in base_url for host in ("localhost", "127.0.0.1", "[::1]"))
