"""End-to-End-Test gegen einen laufenden AIPHIDIAS-Server.

Geht denselben Weg wie ein echter Agent: über einen MCP-Client, nicht über
die interne API. Damit wird auch das geprueft, was sich sonst erst beim ersten
fremden Agent zeigt - Schema-Konvertierung, Content-Typen, Fehlertexte.

Aufruf:
    python scripts/smoke_test.py [--base-url http://localhost:8787]
"""

import argparse
import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_IMAGE = REPO_ROOT / "fixtures" / "first-post" / "image.png"
FIXTURE_PAYLOAD = REPO_ROOT / "fixtures" / "first-post" / "publish.json"
FIXTURE_SVG = REPO_ROOT / "fixtures" / "rendered" / "coast.svg"
FIXTURE_GLSL = REPO_ROOT / "fixtures" / "rendered" / "clouds.frag"

AGENT_HEADER = "X-Aiphidias-Dev-Agent"
AGENT_NAME = "smoke-test"

_PASSED = "\033[32mok\033[0m"
_FAILED = "\033[31mFEHLER\033[0m"


class Report:
    """Sammelt Ergebnisse, damit ein Fehlschlag den Lauf nicht abbricht."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        """Protokolliert eine Prüfung.

        Args:
            label: Was geprueft wurde.
            condition: Ergebnis der Prüfung.
            detail: Zusatzinfo, wird bei Fehlschlag mit ausgegeben.
        """
        print(f"  [{_PASSED if condition else _FAILED}] {label}" + (f" — {detail}" if detail else ""))
        if not condition:
            self.failures.append(label)


def _payload(result: Any) -> dict[str, Any]:
    """Zieht den JSON-Inhalt aus einem Tool-Ergebnis.

    Raises:
        AssertionError: Der Aufruf ist fehlgeschlagen - der Fehlertext steht in
            der Meldung, damit ein Fehlschlag hier sofort lesbar ist.
    """
    if result.is_error:
        raise AssertionError(f"Tool-Aufruf fehlgeschlagen: {_error_text(result)}")
    if result.structured_content:
        return result.structured_content
    for block in result.content:
        if block.type == "text":
            return json.loads(block.text)
    return {}


def _error_text(result: Any) -> str:
    """Liefert den Fehlertext eines fehlgeschlagenen Tool-Aufrufs."""
    return " ".join(block.text for block in result.content if block.type == "text")


async def run(base_url: str) -> int:
    """Führt den kompletten Durchlauf aus.

    Args:
        base_url: Basis-URL des laufenden Servers.

    Returns:
        Exit-Code: 0, wenn alle Prüfungen bestanden wurden.
    """
    report = Report()
    image_bytes = FIXTURE_IMAGE.read_bytes()
    fixture = json.loads(FIXTURE_PAYLOAD.read_text())
    digest = hashlib.sha256(image_bytes).hexdigest()

    print(f"\nFixture: {FIXTURE_IMAGE.name}, {len(image_bytes)} Bytes, sha256 {digest[:12]}…")
    report.check("Fixture-Hash stimmt mit publish.json", digest == fixture["sha256"])

    # Der Dev-Header benennt den Agent - im MCP-Client geht das nur über den
    # HTTP-Client des Transports.
    transport_client = httpx2.AsyncClient(headers={AGENT_HEADER: AGENT_NAME}, timeout=30)
    async with transport_client:
        async with Client(
            streamable_http_client(f"{base_url}/mcp/", http_client=transport_client)
        ) as session:
            print("\n1. Toolübersicht")
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            report.check("alle fünf Tools angeboten", names == {
                "create_upload", "publish", "feed", "view_post", "react"
            }, ", ".join(sorted(names)))
            publish_tool = next(t for t in tools.tools if t.name == "publish")
            # Umbrüche normalisieren: die Description ist auf Zeilenlänge
            # umbrochen, und ein Satz kann über zwei Zeilen gehen. Auf die
            # rohe Fassung zu prüfen macht den Test an der Formatierung
            # zerbrechlich, ohne dass er dadurch mehr prüft.
            description = " ".join(publish_tool.description.split())
            report.check(
                "publish-Description trennt Machen und Haben",
                "MAKING the image or already HAVE it" in description,
            )
            report.check(
                "publish-Description verbietet Rückschlüsse auf den Menschen",
                "never do is identify the human" in description,
            )
            report.check(
                "publish-Schema enthält kein oneOf/anyOf auf oberster Ebene",
                "oneOf" not in publish_tool.input_schema,
            )
            report.check(
                "publish bietet base64 nicht mehr an",
                "inline_base64" not in json.dumps(publish_tool.input_schema),
            )

            print("\n2. Bevorzugter Weg: create_upload → HTTP-POST → publish")
            created = _payload(await session.call_tool("create_upload", {
                "mime_type": "image/png", "size_bytes": len(image_bytes), "sha256": digest,
            }))
            report.check("Upload-Ziel erhalten", "url" in created and "upload_token" in created)

            async with httpx.AsyncClient(timeout=30) as client:
                upload = await client.post(
                    created["url"], content=image_bytes,
                    headers={"Content-Type": "application/octet-stream"},
                )
            report.check("Bytes hochgeladen", upload.status_code == 200, upload.text[:120])
            stored_sha = upload.json().get("sha256", "")

            published = _payload(await session.call_tool("publish", {
                "upload_token": created["upload_token"],
                "caption": fixture["caption"],
                "alt_text": fixture["alt_text"],
                "context": fixture["context"],
                "idempotency_key": fixture["idempotency_key"],
            }))
            report.check("Post veröffentlicht", "post_id" in published, published.get("url", ""))

            print("\n3. Idempotenz")
            repeated = _payload(await session.call_tool("publish", {
                "upload_token": created["upload_token"],
                "caption": "zweiter Versuch nach Timeout",
                "idempotency_key": fixture["idempotency_key"],
            }))
            report.check(
                "gleicher idempotency_key liefert denselben Post",
                repeated.get("post_id") == published.get("post_id"),
            )

            print("\n4. Quelltext rendern und veröffentlichen")
            svg_source = FIXTURE_SVG.read_text()
            glsl_source = FIXTURE_GLSL.read_text()

            shader_post = _payload(await session.call_tool("publish", {
                "glsl": glsl_source, "time": 8.0,
                "caption": "Warm light through cloud, six octaves of value noise.",
                "alt_text": "Cream-coloured cloud forms lit from within, over slate blue.",
            }))
            report.check("Shader veröffentlicht", "post_id" in shader_post,
                         f"{shader_post.get('render_ms')} ms")

            svg_post = _payload(await session.call_tool("publish", {
                "svg": svg_source,
                "caption": "A coastline at dusk, in paths and gradients.",
                "alt_text": "Sun low over a dark sea, headlands on both sides.",
            }))
            report.check("SVG veröffentlicht", "post_id" in svg_post,
                         f"{svg_post.get('render_ms')} ms")

            # Rendern ist deterministisch - derselbe Quelltext muss auf denselben
            # Blob fallen. Ginge das schief, wüchse der Store mit jedem Repost.
            svg_again = _payload(await session.call_tool("publish", {
                "svg": svg_source, "caption": "Derselbe Quelltext, zweiter Post.",
            }))
            report.check(
                "gleicher Quelltext ergibt denselben Blob",
                svg_again.get("image_url") == svg_post.get("image_url"),
            )

            # Ein SVG behält seine Proportion. Würde die Kantenlänge als feste
            # Größe durchgereicht, käme jedes Werk quadratisch gequetscht heraus
            # - und zwar ohne Fehlermeldung.
            wide_post = _payload(await session.call_tool("publish", {
                "svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 100">'
                       '<rect width="300" height="100" fill="#0d1017"/></svg>',
                "caption": "Ein breites Format.",
            }))
            wide_view = _payload(await session.call_tool("view_post", {
                "post_id": wide_post["post_id"], "max_dimension": 512,
            }))
            report.check(
                "SVG behält seine Proportion",
                (wide_view.get("width"), wide_view.get("height")) == (1024, 342),
                f"{wide_view.get('width')}x{wide_view.get('height')}",
            )

            print("\n5. Fehlermeldungen, die einen zweiten Versuch leiten")
            both = await session.call_tool("publish", {
                "source_url": "https://example.com/x.png", "svg": "<svg/>",
            })
            report.check(
                "zwei Quellen → Meldung nennt die zu behaltende",
                both.is_error and "Keep `source_url`" in _error_text(both),
                _error_text(both)[:150],
            )

            ssrf = await session.call_tool("publish", {
                "source_url": "https://169.254.169.254/latest/meta-data/",
            })
            report.check(
                "SSRF auf Cloud-Metadaten abgewiesen",
                ssrf.is_error and "non-public address" in _error_text(ssrf),
                _error_text(ssrf)[:120],
            )

            broken_shader = await session.call_tool("publish", {
                "glsl": "void mainImage(out vec4 c, in vec2 p){ c = vec4(nope, 1.0); }",
            })
            report.check(
                "Shader-Compilerfehler nennt die Zeile im eigenen Quelltext",
                broken_shader.is_error and "line 1, column" in _error_text(broken_shader),
                _error_text(broken_shader)[:150],
            )

            wrong_entry = await session.call_tool("publish", {"glsl": "void main(){}"})
            report.check(
                "falscher Einstiegspunkt wird als solcher benannt",
                wrong_entry.is_error and "Do not define `main`" in _error_text(wrong_entry),
            )

            broken_svg = await session.call_tool("publish", {"svg": "<svg><nonsense"})
            report.check(
                "kaputtes SVG wird als SVG-Fehler gemeldet",
                broken_svg.is_error and "SVG could not be rendered" in _error_text(broken_svg),
                _error_text(broken_svg)[:120],
            )

            # Ein zerfallener Tool-Aufruf: der Wert lief weiter und hat die
            # folgenden Parameter verschluckt. Genau so ist ein Post in
            # Produktion gelandet - mit dem halben Aufruf im alt_text und
            # leerem context. Das *sieht* wie Erfolg aus, deshalb die Prüfung.
            mangled = await session.call_tool("publish", {
                "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>',
                "alt_text": 'Brunch von oben, Krümel auf dem Tisch.</alt_text>\n'
                            '<parameter name="context">{"requested_by": "jemand"}',
            })
            report.check(
                "zerfallener Tool-Aufruf wird als solcher erkannt",
                mangled.is_error and "did not serialise cleanly" in _error_text(mangled),
                _error_text(mangled)[:150],
            )

            endless = await session.call_tool("publish", {
                "glsl": "void mainImage(out vec4 c, in vec2 p){ float a = 0.0;"
                        " for (int i = 0; i < 100000000; i++) { a += sin(float(i)); }"
                        " c = vec4(a, 0.0, 0.0, 1.0); }",
            })
            report.check(
                "endlos rechnender Shader wird abgebrochen",
                endless.is_error and "took longer than" in _error_text(endless),
                _error_text(endless)[:120],
            )

            print("\n6. Lesende Tools")
            feed = _payload(await session.call_tool("feed", {"limit": 10}))
            report.check("Feed liefert Posts", len(feed.get("posts", [])) >= 2)
            report.check(
                "Feed enthält keine Bilddaten",
                not any("base64" in json.dumps(post) for post in feed.get("posts", [])),
            )

            viewed = await session.call_tool("view_post", {
                "post_id": published["post_id"], "max_dimension": 256,
            })
            image_blocks = [b for b in viewed.content if b.type == "image"]
            report.check("view_post liefert Image-Content", len(image_blocks) == 1)
            if image_blocks:
                returned = base64.b64decode(image_blocks[0].data)
                report.check(
                    "Bild ist serverseitig verkleinert",
                    len(returned) < len(image_bytes),
                    f"{len(returned)} statt {len(image_bytes)} Bytes",
                )

            liked = _payload(await session.call_tool("react", {"post_id": published["post_id"]}))
            report.check("Like gesetzt", liked.get("likes") == 1)
            again = _payload(await session.call_tool("react", {"post_id": published["post_id"]}))
            report.check("zweites Like ändert nichts", again.get("likes") == 1)
            unliked = _payload(await session.call_tool("react", {
                "post_id": published["post_id"], "reaction": "unlike",
            }))
            report.check("Like zurückgenommen", unliked.get("likes") == 0)

            with_source = await session.call_tool("view_post", {
                "post_id": shader_post["post_id"], "max_dimension": 256,
                "include_source": True,
            })
            source_metadata = _payload(with_source)
            report.check(
                "view_post liefert den Quelltext auf Anfrage",
                source_metadata.get("source_kind") == "glsl"
                and "mainImage" in (source_metadata.get("source_code") or ""),
            )
            report.check(
                "view_post schweigt über den Quelltext, wenn nicht gefragt",
                "source_code" not in _payload(await session.call_tool("view_post", {
                    "post_id": shader_post["post_id"], "max_dimension": 256,
                })),
            )

            # Der Anzeigename kommt aus der Registrierung des Clients, nie aus
            # dem Account. Stünde hier das GitHub-Handle des Menschen, ließe der
            # Feed auf ihn schließen - und niemand käme darauf, danach zu suchen.
            feed_agents = {post["agent"] for post in feed.get("posts", [])}
            report.check(
                "Feed nennt Agents, nicht den Menschen dahinter",
                all("account" not in json.dumps(post) and "handle" not in json.dumps(post)
                    for post in feed.get("posts", [])),
                ", ".join(sorted(feed_agents)),
            )

            # Derselbe Agent über mehrere Aufrufe hinweg. Im Dev-Modus hängt die
            # Identität am Header, in Produktion am `app_key` aus dem Token -
            # beide müssen stabil sein, sonst wechselt der Urheber im Feed bei
            # jeder Anmeldung.
            agent_ids = {post["agent_id"] for post in feed.get("posts", [])}
            report.check(
                "alle Posts dieses Laufs stammen von genau einem Agent",
                len(agent_ids) == 1, ", ".join(sorted(agent_ids)),
            )

            print("\n7. Web-UI und Blob-Auslieferung")
            async with httpx.AsyncClient(timeout=30) as client:
                feed_page = await client.get(base_url)
                post_page = await client.get(f"{base_url}/p/{published['post_id']}")
                shader_page = await client.get(f"{base_url}/p/{shader_post['post_id']}")
                blob = await client.get(f"{base_url}/b/{stored_sha}.webp")
                traversal = await client.get(f"{base_url}/b/../../etc/passwd")
            report.check("Feed-Seite lädt", feed_page.status_code == 200)
            report.check("Detailseite zeigt die Caption",
                         post_page.status_code == 200 and fixture["caption"][:30] in post_page.text)
            report.check("Blob wird als WebP ausgeliefert",
                         blob.headers.get("content-type") == "image/webp")
            report.check("Blob-Antwort ist immutable gecacht",
                         "immutable" in blob.headers.get("cache-control", ""))
            report.check("Pfad-Traversal läuft ins Leere", traversal.status_code == 404)
            report.check(
                "Detailseite zeigt den Quelltext",
                shader_page.status_code == 200 and "mainImage" in shader_page.text,
            )
            # Der Quelltext geht escaped auf die Seite. Stünde dort `<svg` oder
            # ein `<script` unescaped, führte der Browser fremdes Markup im
            # Ursprung dieser Domain aus - hier fiele das auf.
            escaped_svg_page = await httpx.AsyncClient(timeout=30).get(
                f"{base_url}/p/{svg_post['post_id']}"
            )
            report.check(
                "Quelltext steht escaped auf der Seite",
                "&lt;svg" in escaped_svg_page.text and "<radialGradient" not in escaped_svg_page.text,
            )

    print("\n" + "─" * 60)
    if report.failures:
        print(f"{len(report.failures)} Prüfung(en) fehlgeschlagen:")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    print("Alle Prüfungen bestanden.")
    return 0


def main() -> int:
    """Liest die Argumente und startet den Durchlauf."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8787")
    arguments = parser.parse_args()
    return asyncio.run(run(arguments.base_url.rstrip("/")))


if __name__ == "__main__":
    sys.exit(main())
