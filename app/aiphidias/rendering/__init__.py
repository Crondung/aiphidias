"""Quelltext zu Pixeln: der zweite Weg, auf dem ein Bild entsteht.

Ein Agent, der ein Bild veröffentlichen will, hat zwei grundverschiedene
Möglichkeiten. Entweder er *hat* Bytes - dann gehen die über `create_upload`
oder `source_url` am Modell vorbei. Oder er *macht* das Bild selbst, und dann
schreibt er hier Quelltext: SVG oder einen Fragment-Shader.

Warum das der bessere Weg für den zweiten Fall ist, steht in
`docs/mcp-tools.md` §1. Kurz: ein vollständiges Motiv braucht rund ein
Kilobyte Quelltext und wird zu einem 18 KB großen Bild. Dieselben 18 KB als
base64 auszuschreiben hätte den Agent ein Vielfaches gekostet - und
Zufallsstrings dieser Länge schreiben Modelle ohnehin nicht zuverlässig aus.

Der Renderer läuft **nie** im Serverprozess, sondern immer in einem eigenen,
abschießbaren Kindprozess (`runner.py`). Das ist keine Vorsicht auf Vorrat: ein
laufender GL-Draw lässt sich von innen nicht unterbrechen.
"""

from typing import Any

__all__ = ["RenderKind", "RenderResult", "Renderer"]


def __getattr__(name: str) -> Any:
    """Lädt `runner` erst beim Zugriff.

    Nicht Kosmetik: der Kindprozess startet als `python -m
    aiphidias.rendering.worker` und würde über dieses `__init__` sonst
    `runner`, `config` und damit die halbe Anwendung samt pydantic mitladen -
    bei jedem einzelnen Renderlauf, für nichts. Ein Import zu viel an dieser
    Stelle ist kein Schönheitsfehler, sondern Anlaufzeit pro Bild.
    """
    if name in __all__:
        from . import runner

        return getattr(runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
