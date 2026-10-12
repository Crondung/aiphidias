"""Der eine Weg, auf dem ein Bild in die Ablage kommt.

Zwei Eingänge, ein Weg. `store_image_bytes` nimmt fremde Bytes entgegen - aus
`source_url` oder aus einem eingelösten Upload-Token - und prüft sie
entsprechend misstrauisch. `store_rendered_image` nimmt, was `rendering/` aus
Quelltext erzeugt hat, und lässt die Prüfungen weg, die dort gegenstandslos
wären.

Ab `_persist` laufen beide durch denselben Code. Genau darum geht es: Ablage,
Thumbnail und Deduplizierung existieren einmal und nicht zweimal leicht
unterschiedlich.
"""

import logging
from dataclasses import dataclass

import asyncpg

from .blobs import BlobStore, sha256_hex
from .config import Settings
from .errors import ValidationError
from .imaging import EncodedImage, downscale, encode_for_storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoredImage:
    """Ergebnis eines Ingest-Laufs.

    Attributes:
        sha256: Hash des re-encodierten Bildes - zugleich sein Ablageort.
        width: Breite des gespeicherten Vollbildes.
        height: Höhe des gespeicherten Vollbildes.
        byte_size: Größe des gespeicherten Vollbildes.
        deduplicated: True, wenn genau diese Bytes schon im Store lagen.
    """

    sha256: str
    width: int
    height: int
    byte_size: int
    deduplicated: bool


async def store_image_bytes(
    conn: asyncpg.Connection,
    raw: bytes,
    *,
    blob_store: BlobStore,
    settings: Settings,
    expected_sha256: str | None = None,
) -> StoredImage:
    """Nimmt rohe Bytes entgegen und legt das normalisierte Bild ab.

    Args:
        conn: Offene Verbindung für den `blobs`-Eintrag.
        raw: Die rohen Bytes des Originals.
        blob_store: Zielablage.
        settings: Grenzwerte für Größe und Re-Encoding.
        expected_sha256: Optionale Zusicherung des Agents über die *Original*-Bytes.

    Returns:
        Beschreibung des gespeicherten Bildes.

    Raises:
        ValidationError: Bytes leer, zu groß oder Hash stimmt nicht.
        ImageError: Bytes sind kein verarbeitbares Bild.
    """
    if not raw:
        raise ValidationError("The image is empty - no bytes were received.")
    if len(raw) > settings.max_upload_bytes:
        raise ValidationError(
            f"The image is {len(raw)} bytes, the limit is {settings.max_upload_bytes} bytes."
        )

    if expected_sha256 is not None:
        actual = sha256_hex(raw)
        if actual != expected_sha256.lower():
            raise ValidationError(
                f"sha256 mismatch: you declared {expected_sha256}, the received bytes hash "
                f"to {actual}. The upload was rejected; retry with the correct bytes or hash."
            )

    encoded = encode_for_storage(raw, settings)
    return await _persist(conn, encoded, blob_store=blob_store, settings=settings)


async def store_rendered_image(
    conn: asyncpg.Connection,
    encoded: EncodedImage,
    *,
    blob_store: BlobStore,
    settings: Settings,
) -> StoredImage:
    """Legt ein Bild ab, das der Server selbst gerendert hat.

    Der zweite Eingang neben `store_image_bytes` - ab `_persist` läuft beides
    durch denselben Code, damit Ablage, Thumbnail und Deduplizierung genau
    einmal existieren.

    Was hier fehlt, fehlt mit Absicht: keine Formatprüfung, keine Größengrenze,
    kein Bomb-Schutz. Diese Prüfungen richten sich gegen fremde Bytes. Hier ist
    das Bild in `rendering/` entstanden, in einem Prozess mit Zeit- und
    Speichergrenze, in einer Größe, die wir selbst vorgegeben haben.

    Args:
        conn: Offene Verbindung für den `blobs`-Eintrag.
        encoded: Das fertig encodierte Bild aus dem Renderer.
        blob_store: Zielablage.
        settings: Grenzwerte für das Thumbnail.

    Returns:
        Beschreibung des gespeicherten Bildes. `deduplicated` ist wahr, wenn
        derselbe Quelltext schon einmal dasselbe Bild ergeben hat - das Rendern
        ist deterministisch, deshalb greift die Deduplizierung auch hier.
    """
    return await _persist(conn, encoded, blob_store=blob_store, settings=settings)


async def _persist(
    conn: asyncpg.Connection,
    encoded: EncodedImage,
    *,
    blob_store: BlobStore,
    settings: Settings,
) -> StoredImage:
    """Schreibt Vollbild und Thumbnail und trägt den Blob in die DB ein.

    Der Hash ist der des re-encodierten Bildes. Nur so greift die
    Deduplizierung: jedes Original trägt eigene Metadaten und hätte einen
    eigenen Hash, auch wenn das sichtbare Bild identisch ist.
    """
    digest = sha256_hex(encoded.data)
    already_stored = await conn.fetchval("SELECT true FROM blobs WHERE sha256 = $1", digest)

    if already_stored and blob_store.exists(digest):
        return StoredImage(
            sha256=digest,
            width=encoded.width,
            height=encoded.height,
            byte_size=len(encoded.data),
            deduplicated=True,
        )

    blob_store.write(digest, encoded.data)
    # Das Thumbnail entsteht sofort mit, damit Feed und `view_post` nie auf
    # eine Bildverarbeitung im Request-Pfad warten müssen.
    thumbnail = downscale(encoded.data, settings.thumbnail_edge_px, settings.webp_quality)
    blob_store.write(digest, thumbnail, thumbnail=True)

    await conn.execute(
        """
        INSERT INTO blobs (sha256, mime_type, byte_size, width, height)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (sha256) DO NOTHING
        """,
        digest,
        encoded.mime_type,
        len(encoded.data),
        encoded.width,
        encoded.height,
    )

    logger.info("Blob abgelegt: %s (%d Bytes, %dx%d)", digest, len(encoded.data), encoded.width, encoded.height)
    return StoredImage(
        sha256=digest,
        width=encoded.width,
        height=encoded.height,
        byte_size=len(encoded.data),
        deduplicated=bool(already_stored),
    )
