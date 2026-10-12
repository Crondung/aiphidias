"""SVG rastern - mit resvg, und zwar bewusst mit resvg.

SVG ist kein Bildformat, sondern ein Dokumentformat: es kennt Skripte,
`<foreignObject>` mit HTML darin und Verweise auf externe Dateien und URLs. Ein
Renderer, der das alles unterstützt, ist eine Angriffsfläche.

resvg unterstützt es nicht. Es hat keinen Netzwerkstack und keine
Skriptausführung, und ohne `resources_dir` kommt es auch nicht ans
Dateisystem. Nachgemessen mit einem SVG, das ein `<image>` auf die
Metadaten-IP der Cloud und ein `<script>` enthielt: das Ergebnis war ein leeres
Bild, in 0,3 Millisekunden, ohne eine einzige ausgehende Verbindung.

Dieses Modul wird ausschließlich vom Kindprozess importiert (`worker.py`).
"""

import io

import resvg_py
from PIL import Image, UnidentifiedImageError

# Sichtbarer Grund für alles Transparente. Der Rest der Pipeline arbeitet in
# RGB (siehe imaging._flatten_to_rgb); ein Werk mit Alphakanal sähe im Feed je
# nach Hintergrund anders aus als beim Urheber.
BACKGROUND = "#ffffff"


def render_svg(
    source: str, width: int, height: int, font_dir: str | None
) -> tuple[bytes, int, int]:
    """Rastert SVG-Quelltext zu rohen RGB-Bytes.

    `width` und `height` spannen einen Rahmen auf, in den das Bild **passt** -
    sie erzwingen keine Maße. Ein SVG mit einer 300x100-`viewBox` bleibt bei
    300x100, auch wenn hier 1024x1024 steht. Das ist richtig so: die Proportion
    gehört zum Werk, und ein in ein Quadrat gequetschtes Bild wäre ein anderes.

    Deshalb gibt diese Funktion die *tatsächlichen* Maße mit zurück, statt die
    angefragten weiterzureichen.

    Args:
        source: Das SVG-Markup.
        width: Breite des Rahmens in Pixeln.
        height: Höhe des Rahmens in Pixeln.
        font_dir: Verzeichnis mit den erlaubten Schriften, oder None für
            schriftlos. Systemschriften werden bewusst nicht gesucht: derselbe
            Quelltext soll überall dasselbe Bild ergeben, denn der Hash des
            Ergebnisses ist sein Ablageort.

    Returns:
        Tripel aus RGB-Bytes sowie der tatsächlichen Breite und Höhe.

    Raises:
        ValueError: Das SVG ließ sich nicht parsen oder nicht rastern. Der Text
            der Ausnahme geht wörtlich an den Agent.
    """
    try:
        png = resvg_py.svg_to_bytes(
            svg_string=source,
            width=width,
            height=height,
            background=BACKGROUND,
            # Beides zusammen ist die Abschottung: keine Systemschriften und -
            # durch das fehlende `resources_dir` - kein Weg zu einer Datei auf
            # der Platte.
            skip_system_fonts=True,
            font_dirs=[font_dir] if font_dir else [],
        )
    except ValueError as exc:
        # resvgs Meldungen sind knapp ("the root node was opened but never
        # closed") und ohne Kontext nicht zuzuordnen. Der Agent soll nicht
        # raten müssen, welcher seiner Parameter gemeint ist.
        raise ValueError(f"The SVG could not be rendered: {exc}") from exc

    try:
        with Image.open(io.BytesIO(bytes(png))) as raster:
            rgb = raster.convert("RGB")
            return rgb.tobytes(), rgb.width, rgb.height
    except (UnidentifiedImageError, OSError) as exc:  # pragma: no cover - resvg liefert valides PNG
        raise ValueError(f"The rendered SVG could not be read back: {exc}") from exc
