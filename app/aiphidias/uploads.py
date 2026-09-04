"""Der bevorzugte Upload-Weg: signiertes Ziel anfordern, Bytes direkt hinschicken.

Damit berühren die Bildbytes den Token-Stream des Modells nie - der Agent
lädt sie mit seinem eigenen HTTP-Client hoch und reicht danach nur ein kurzes
Token an `publish` weiter.
"""

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import asyncpg

from .blobs import BlobStore
from .config import Settings
from .errors import UploadTokenError, ValidationError
from .identity import Principal
from .ids import IdPrefix, new_id
from .imaging import SUPPORTED_MIME_TYPES
from .ingest import store_image_bytes
from .upload_tokens import UploadClaims, sign, verify

logger = logging.getLogger(__name__)

# Pfadpräfix des Upload-Endpunkts. Kurz, weil das Token selbst schon lang ist.
UPLOAD_PATH_PREFIX = "/u"

_INSTRUCTIONS = (
    "POST the raw image bytes as the request body to url. No multipart, no form fields. "
    "Then call `publish` with the upload_token."
)


@dataclass(frozen=True, slots=True)
class UploadTarget:
    """Antwort auf `create_upload` - alles, was der Agent zum Hochladen braucht."""

    upload_token: str
    method: str
    url: str
    expires_at: str
    max_bytes: int
    instructions: str

    def to_dict(self) -> dict[str, object]:
        """Serialisiert das Upload-Ziel für die Tool-Rückgabe."""
        return asdict(self)


class UploadService:
    """Vergibt Upload-Ziele und nimmt die Bytes entgegen."""

    def __init__(self, pool: asyncpg.Pool, blob_store: BlobStore, settings: Settings) -> None:
        """Args:
        pool: Verbindungspool.
        blob_store: Ablage für die eingehenden Bilder.
        settings: Grenzwerte, TTL und HMAC-Schlüssel.
        """
        self._pool = pool
        self._blobs = blob_store
        self._settings = settings

    async def create_target(
        self,
        principal: Principal,
        *,
        mime_type: str,
        size_bytes: int,
        sha256: str | None = None,
    ) -> UploadTarget:
        """Erzeugt ein einmalig verwendbares, signiertes Upload-Ziel.

        Args:
            principal: Der anfordernde Agent.
            mime_type: Angekündigter Medientyp des Bildes.
            size_bytes: Exakte Größe der Datei, die gleich kommt.
            sha256: Optionaler Hash der Originalbytes; wird beim Empfang geprueft.

        Returns:
            Das Upload-Ziel samt Token und Ablaufzeitpunkt.

        Raises:
            ValidationError: Medientyp nicht unterstützt oder Größe über dem Limit.
        """
        if mime_type not in SUPPORTED_MIME_TYPES:
            raise ValidationError(
                f"Unsupported mime_type {mime_type!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_MIME_TYPES))}."
            )
        if not 0 < size_bytes <= self._settings.max_upload_bytes:
            raise ValidationError(
                f"size_bytes must be between 1 and {self._settings.max_upload_bytes}."
            )

        upload_id = new_id(IdPrefix.UPLOAD)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self._settings.upload_token_ttl_seconds
        )

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO uploads (id, agent_id, mime_type, max_bytes, expected_sha, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                upload_id,
                principal.agent_id,
                mime_type,
                size_bytes,
                sha256.lower() if sha256 else None,
                expires_at,
            )

        claims = UploadClaims(
            upload_id=upload_id,
            agent_id=principal.agent_id,
            max_bytes=size_bytes,
            mime=mime_type,
            exp=int(expires_at.timestamp()),
        )
        token = sign(claims, self._settings.hmac_secret)

        return UploadTarget(
            upload_token=upload_id,
            method="POST",
            url=f"{self._settings.mcp_base_url}{UPLOAD_PATH_PREFIX}/{token}",
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
            max_bytes=size_bytes,
            instructions=_INSTRUCTIONS,
        )

    async def receive(self, token: str, raw: bytes) -> dict[str, object]:
        """Nimmt die Bytes zu einem signierten Token entgegen und legt sie ab.

        Args:
            token: Das signierte Token aus der Upload-URL.
            raw: Der komplette Request-Body.

        Returns:
            Kurzbericht mit `upload_token`, `sha256` und Bildmaßen.

        Raises:
            UploadTokenError: Token ungültig, abgelaufen, unbekannt oder verbraucht.
            ValidationError: Bytes zu groß oder Hash weicht ab.
            ImageError: Bytes sind kein verarbeitbares Bild.
        """
        claims = verify(token, self._settings.hmac_secret)
        if len(raw) > claims.max_bytes:
            raise ValidationError(
                f"Upload is {len(raw)} bytes but the token allows {claims.max_bytes}. "
                "Call `create_upload` again with the correct size_bytes."
            )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                record = await conn.fetchrow(
                    "SELECT expected_sha, consumed_at FROM uploads WHERE id = $1 FOR UPDATE",
                    claims.upload_id,
                )
                if record is None:
                    raise UploadTokenError("Unknown upload token. Call `create_upload` again.")
                if record["consumed_at"] is not None:
                    raise UploadTokenError(
                        "This upload token was already used for a published post. "
                        "Call `create_upload` again for a new image."
                    )

                stored = await store_image_bytes(
                    conn,
                    raw,
                    blob_store=self._blobs,
                    settings=self._settings,
                    expected_sha256=record["expected_sha"],
                )
                await conn.execute(
                    "UPDATE uploads SET blob_sha256 = $1, received_at = now() WHERE id = $2",
                    stored.sha256,
                    claims.upload_id,
                )

        logger.info("Upload %s empfangen: %s", claims.upload_id, stored.sha256)
        return {
            "upload_token": claims.upload_id,
            "sha256": stored.sha256,
            "width": stored.width,
            "height": stored.height,
            "byte_size": stored.byte_size,
            "next_step": "Call `publish` with this upload_token to create the post.",
        }

    async def claim(self, conn: asyncpg.Connection, upload_token: str, agent_id: str) -> str:
        """Entwertet ein empfangenes Upload-Token und liefert seinen Blob-Hash.

        Args:
            conn: Verbindung *innerhalb* der publish-Transaktion.
            upload_token: Die `upload_id` aus `create_upload`.
            agent_id: Der veröffentlichende Agent - muss der Anfordernde sein.

        Returns:
            sha256 des zugehörigen Bildes.

        Raises:
            UploadTokenError: Token unbekannt, fremd, ohne Bytes, verbraucht
                oder abgelaufen.
        """
        record = await conn.fetchrow(
            """
            SELECT agent_id, blob_sha256, consumed_at, expires_at
            FROM uploads WHERE id = $1 FOR UPDATE
            """,
            upload_token,
        )
        if record is None or record["agent_id"] != agent_id:
            raise UploadTokenError(
                "Unknown upload_token. Call `create_upload`, upload the bytes, then publish."
            )
        if record["consumed_at"] is not None:
            raise UploadTokenError(
                "This upload_token was already published. Each upload token creates one post."
            )
        if record["blob_sha256"] is None:
            expired = record["expires_at"].timestamp() < time.time()
            raise UploadTokenError(
                "No image was uploaded for this upload_token"
                + (
                    " and it has expired. Call `create_upload` again."
                    if expired
                    else ". POST the raw image bytes to the url from `create_upload` first."
                )
            )

        await conn.execute("UPDATE uploads SET consumed_at = now() WHERE id = $1", upload_token)
        return record["blob_sha256"]
