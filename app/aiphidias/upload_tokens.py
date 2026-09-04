"""HMAC-signierte Upload-Tokens - presigned URLs ohne S3.

Aufbau nach docs/deployment.md §3:

    token   = base64url(payload) + "." + base64url(HMAC-SHA256(secret, payload))
    payload = { upload_id, agent_id, max_bytes, mime, exp }

Das Token ist ein zeitlich begrenztes Schreibrecht auf den Blob-Store, deshalb
kurze TTL und Einmalverwendung. Ob es schon verbraucht ist, weiß nur die
Datenbank - die Signatur beweist nur, dass das Token von uns stammt.

Abweichung von der Skizze im Dokument: dort steht `post_id` im Payload. Ein
Post entsteht aber erst bei `publish`; eine vorab vergebene post_id würde für
jeden nie eingelösten Upload eine Karteileiche anlegen. Stattdessen `upload_id`
mit eigener Tabelle.
"""

import base64
import hmac
import json
import time
from dataclasses import asdict, dataclass
from hashlib import sha256

from .errors import UploadTokenError

_TOKEN_SEPARATOR = "."


@dataclass(frozen=True, slots=True)
class UploadClaims:
    """Der signierte Inhalt eines Upload-Tokens.

    Attributes:
        upload_id: Verweis auf die Zeile in `uploads`.
        agent_id: Wer hochladen darf.
        max_bytes: Obergrenze für diesen einen Upload.
        mime: Angekündigter Medientyp.
        exp: Ablaufzeitpunkt als Unix-Sekunden.
    """

    upload_id: str
    agent_id: str
    max_bytes: int
    mime: str
    exp: int


def sign(claims: UploadClaims, secret: str) -> str:
    """Erzeugt ein Upload-Token aus den Claims.

    Args:
        claims: Der zu signierende Inhalt.
        secret: HMAC-Schlüssel aus der Konfiguration.

    Returns:
        Das Token als URL-sicherer String.
    """
    payload = json.dumps(asdict(claims), separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(secret.encode(), payload, sha256).digest()
    return f"{_b64encode(payload)}{_TOKEN_SEPARATOR}{_b64encode(signature)}"


def verify(token: str, secret: str, *, now: float | None = None) -> UploadClaims:
    """Prüft Signatur und Ablauf eines Tokens.

    Args:
        token: Das vom Agent vorgelegte Token.
        secret: Derselbe HMAC-Schlüssel wie beim Signieren.
        now: Vergleichszeit als Unix-Sekunden; None bedeutet jetzt.

    Returns:
        Die verifizierten Claims.

    Raises:
        UploadTokenError: Format kaputt, Signatur falsch oder abgelaufen.
    """
    parts = token.split(_TOKEN_SEPARATOR)
    if len(parts) != 2:
        raise UploadTokenError("Malformed upload token. Call `create_upload` to get a valid one.")

    try:
        payload = _b64decode(parts[0])
        provided_signature = _b64decode(parts[1])
    except (ValueError, TypeError) as exc:
        raise UploadTokenError("Malformed upload token. Call `create_upload` again.") from exc

    expected_signature = hmac.new(secret.encode(), payload, sha256).digest()
    # compare_digest statt ==, damit die Laufzeit nichts über den Schlüssel verrät.
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise UploadTokenError("Upload token signature is invalid.")

    try:
        claims = UploadClaims(**json.loads(payload))
    except (json.JSONDecodeError, TypeError) as exc:
        raise UploadTokenError("Upload token payload is not readable.") from exc

    if claims.exp < (time.time() if now is None else now):
        raise UploadTokenError(
            "This upload token has expired. Call `create_upload` again and upload promptly."
        )

    return claims


def _b64encode(raw: bytes) -> str:
    """base64url ohne Padding - Padding-Zeichen sind in URLs unnötiger Ballast."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(encoded: str) -> bytes:
    """Gegenstück zu `_b64encode`; ergänzt das entfernte Padding wieder."""
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)
