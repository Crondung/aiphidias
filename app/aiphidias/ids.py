"""Erzeugung öffentlicher Objekt-IDs.

Präfixe machen IDs in Logs und Fehlermeldungen selbsterklärend: an `p_7QcM…`
sieht man ohne Nachschlagen, dass ein Post gemeint ist.
"""

import secrets
from enum import StrEnum

# 96 Bit Zufall. Genug, dass IDs nicht erratbar sind, kurz genug zum Vorlesen.
_ID_ENTROPY_BYTES = 12


class IdPrefix(StrEnum):
    """Präfixe der öffentlich sichtbaren IDs."""

    ACCOUNT = "ac"
    AGENT = "ag"
    POST = "p"
    UPLOAD = "ul"


def new_id(prefix: IdPrefix) -> str:
    """Erzeugt eine neue, zufällige ID mit dem angegebenen Präfix.

    Args:
        prefix: Objektart, für die die ID gilt.

    Returns:
        ID der Form `<prefix>_<urlsafe-base64>`, z. B. `p_7QcMv1a…`.
    """
    return f"{prefix.value}_{secrets.token_urlsafe(_ID_ENTROPY_BYTES)}"
