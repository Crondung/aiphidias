"""Kurzlebige, signierte Zustandswerte für den GitHub-Umweg.

Der OAuth-`state`-Parameter muss zweierlei leisten: den angefangenen
Hydra-Login wiederfinden und beweisen, dass der Callback zu genau dem Browser
gehört, der den Flow gestartet hat. Deshalb trägt der signierte Wert einen
Nonce, dessen Gegenstück als Cookie im Browser liegt - ein signierter Wert
allein wäre gegen Login-CSRF wirkungslos.

Bewusst dieselbe Bauart wie `upload_tokens.py` im Hauptdienst, aber eine eigene
Kopie: die beiden Dienste werden getrennt deployt, und eine geteilte Bibliothek
für dreißig Zeilen wäre mehr Kopplung, als sie einspart.
"""

import base64
import hmac
import json
import time
from hashlib import sha256

_SEPARATOR = "."


class StateError(ValueError):
    """Der `state`-Wert ist ungültig, abgelaufen oder gehört zu einem anderen Browser.

    Die Meldungen sind englisch, weil sie auf der Fehlerseite landen, die ein
    Mensch zu sehen bekommt - siehe die Sprachregel in CLAUDE.md.
    """


def sign_state(payload: dict[str, object], secret: str, ttl_seconds: int) -> str:
    """Signiert einen Zustandswert mit Ablaufzeit.

    Args:
        payload: Die einzupackenden Werte.
        secret: HMAC-Schlüssel.
        ttl_seconds: Gültigkeitsdauer ab jetzt.

    Returns:
        Der signierte, URL-sichere Wert.
    """
    body = json.dumps({**payload, "exp": int(time.time()) + ttl_seconds}, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(secret.encode(), body, sha256).digest()
    return f"{_b64encode(body)}{_SEPARATOR}{_b64encode(signature)}"


def verify_state(value: str, secret: str) -> dict[str, object]:
    """Prüft Signatur und Ablauf eines Zustandswerts.

    Args:
        value: Der zu prüfende Wert.
        secret: Derselbe HMAC-Schlüssel wie beim Signieren.

    Returns:
        Der ursprüngliche Inhalt ohne das `exp`-Feld.

    Raises:
        StateError: Format kaputt, Signatur falsch oder abgelaufen.
    """
    parts = value.split(_SEPARATOR)
    if len(parts) != 2:
        raise StateError("malformed value")

    try:
        body = _b64decode(parts[0])
        provided = _b64decode(parts[1])
    except (ValueError, TypeError) as exc:
        raise StateError("not decodable") from exc

    expected = hmac.new(secret.encode(), body, sha256).digest()
    if not hmac.compare_digest(expected, provided):
        raise StateError("signature mismatch")

    payload = json.loads(body)
    if payload.get("exp", 0) < time.time():
        raise StateError("it expired")

    payload.pop("exp", None)
    return payload


def _b64encode(raw: bytes) -> str:
    """base64url ohne Padding."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(encoded: str) -> bytes:
    """Gegenstück zu `_b64encode`."""
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
