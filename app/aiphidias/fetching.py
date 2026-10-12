"""Serverseitiges Holen einer `source_url` - der SSRF-anfällige Pfad.

Der Server holt hier eine URL, die ein beliebiger Agent bestimmt. Ein
Blocklist-Check auf dem URL-String reicht nicht: ein DNS-Name kann auf eine
interne Adresse zeigen, und ein Redirect kann von außen nach innen führen.

Deshalb (docs/mcp-tools.md §8):

* nur https,
* Hostname auflösen und **jede** aufgelöste Adresse gegen private Bereiche prüfen,
* Redirects einzeln nachprüfen statt ihnen blind zu folgen,
* Größe beim Streamen erzwingen, nicht erst danach.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from .config import Settings
from .errors import SourceFetchError

logger = logging.getLogger(__name__)

_ALLOWED_SCHEME = "https"
# Ein Vielfaches der Chunk-Größe ist irrelevant; 64 KB ist ein
# üblicher Kompromiss zwischen Syscalls und Speicher.
_STREAM_CHUNK_BYTES = 64 * 1024


async def fetch_image(url: str, settings: Settings) -> bytes:
    """Holt Bildbytes von einer öffentlich erreichbaren https-URL.

    Args:
        url: Die vom Agent genannte Quelle.
        settings: Konfiguration mit Timeout, Redirect- und Größenlimit.

    Returns:
        Die rohen Bytes. Ob es ein gültiges Bild ist, entscheidet `imaging`.

    Raises:
        SourceFetchError: Ziel verweigert, nicht erreichbar oder zu groß.
    """
    remaining_redirects = settings.source_fetch_max_redirects
    current_url = url

    async with httpx.AsyncClient(
        timeout=settings.source_fetch_timeout_seconds, follow_redirects=False
    ) as client:
        while True:
            _assert_public_target(current_url)
            try:
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or remaining_redirects <= 0:
                            raise SourceFetchError(
                                "source_url redirected too many times. "
                                "Pass a direct link to the image file."
                            )
                        remaining_redirects -= 1
                        current_url = str(response.url.join(location))
                        continue

                    if response.status_code != httpx.codes.OK:
                        raise SourceFetchError(
                            f"source_url returned HTTP {response.status_code}. "
                            "The URL must be publicly reachable without authentication."
                        )

                    return await _read_capped(response, settings.max_upload_bytes)
            except httpx.HTTPError as exc:
                raise SourceFetchError(
                    f"source_url could not be fetched: {exc}. "
                    "Check the URL, or upload the bytes yourself via `create_upload`."
                ) from exc


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Liest den Body und bricht ab, sobald das Limit überschritten wird.

    Der `Content-Length`-Header wird bewusst nicht als Zusicherung behandelt -
    er kann fehlen oder lügen.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise SourceFetchError(
                f"The image at source_url exceeds the limit of {max_bytes} bytes."
            )
        chunks.append(chunk)

    if total == 0:
        raise SourceFetchError("source_url returned an empty response body.")
    return b"".join(chunks)


def _assert_public_target(url: str) -> None:
    """Prüft Schema und Zieladressen einer URL vor dem Verbindungsaufbau.

    Args:
        url: Die zu prüfende URL.

    Raises:
        SourceFetchError: Kein https, kein auflösbarer Host, oder der Host
            zeigt auf eine nicht-öffentliche Adresse.
    """
    parsed = urlparse(url)
    if parsed.scheme != _ALLOWED_SCHEME:
        raise SourceFetchError("source_url must be an https URL.")
    if not parsed.hostname:
        raise SourceFetchError("source_url has no host.")

    try:
        resolved = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SourceFetchError(f"source_url host could not be resolved: {parsed.hostname}") from exc

    for *_, sockaddr in resolved:
        address = ipaddress.ip_address(sockaddr[0])
        if not _is_public(address):
            logger.warning("SSRF-Versuch blockiert: %s -> %s", parsed.hostname, address)
            raise SourceFetchError(
                "source_url resolves to a non-public address and was refused. "
                "Use a publicly reachable URL."
            )


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Sagt, ob eine Adresse aus dem öffentlichen Internet stammt.

    Deckt RFC1918, Loopback, Link-Local (inkl. Cloud-Metadaten unter
    169.254.169.254), Multicast und reservierte Bereiche ab.
    """
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )
