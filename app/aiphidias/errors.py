"""Fehler, die Agents lesen.

Fehlermeldungen sind Teil der API-Oberfläche: ein Agent liest sie und startet
daraufhin einen zweiten Versuch. Deshalb sind sie englisch (wie die
Tool-Descriptions), nennen die Ursache und sagen, was stattdessen zu tun ist.
"""


class AiphidiasError(Exception):
    """Basis für alle fachlichen Fehler.

    Die Nachricht wird unverändert an den aufrufenden Agent durchgereicht und
    muss deshalb handlungsleitend formuliert sein.
    """


class ValidationError(AiphidiasError):
    """Die Eingabe des Agents ist unbrauchbar - er kann es korrigiert erneut versuchen."""


class NotFoundError(AiphidiasError):
    """Angefordertes Objekt existiert nicht (oder nicht mehr)."""


class UploadTokenError(ValidationError):
    """Upload-Token fehlt, ist abgelaufen, gefälscht oder schon verbraucht."""


class ImageError(ValidationError):
    """Bytes ließen sich nicht als unterstütztes Bild verarbeiten."""


class SourceFetchError(ValidationError):
    """`source_url` konnte nicht geholt werden - inkl. absichtlich verweigerter Ziele."""
