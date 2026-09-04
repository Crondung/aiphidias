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
            report.check(
                "publish-Description nennt die Rangfolge der Bytequellen",
                "Use the FIRST one your" in publish_tool.description,
            )
            report.check(
                "publish-Schema enthält kein oneOf/anyOf auf oberster Ebene",
                "oneOf" not in publish_tool.input_schema,
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

            print("\n4. Notnagel-Pfad: inline_base64")
            small = _shrink_to_inline_limit(image_bytes)
            inline = _payload(await session.call_tool("publish", {
                "inline_base64": base64.b64encode(small).decode(),
                "caption": "Kleines Bild über den Notnagel-Pfad.",
                "alt_text": "Verkleinerte Fassung des Fixture-Bildes.",
            }))
            report.check("inline_base64 veröffentlicht", "post_id" in inline)

            print("\n5. Fehlermeldungen, die einen zweiten Versuch leiten")
            both = await session.call_tool("publish", {
                "source_url": "https://example.com/x.png", "inline_base64": "AAAA",
            })
            report.check(
                "zwei Quellen → Meldung nennt die zu bevorzugende",
                both.is_error and "Use `source_url`" in _error_text(both),
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

            not_an_image = await session.call_tool("publish", {
                "inline_base64": base64.b64encode(b"definitiv kein Bild").decode(),
            })
            report.check(
                "Nicht-Bild abgewiesen",
                not_an_image.is_error and "not a supported image" in _error_text(not_an_image),
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

            print("\n7. Web-UI und Blob-Auslieferung")
            async with httpx.AsyncClient(timeout=30) as client:
                feed_page = await client.get(base_url)
                post_page = await client.get(f"{base_url}/p/{published['post_id']}")
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

    print("\n" + "─" * 60)
    if report.failures:
        print(f"{len(report.failures)} Prüfung(en) fehlgeschlagen:")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    print("Alle Prüfungen bestanden.")
    return 0


def _shrink_to_inline_limit(image_bytes: bytes) -> bytes:
    """Verkleinert das Fixture-Bild, bis es unter die base64-Grenze passt.

    Die Fixture liegt mit 93 KB knapp unter der 128-KB-Grenze für base64 -
    knapp genug, dass sie sich zum Testen des Grenzfalls eignet, aber nicht
    verlässlich darunter. Deshalb hier definiert verkleinern.
    """
    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as source:
        source.thumbnail((256, 256))
        buffer = io.BytesIO()
        source.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()


def main() -> int:
    """Liest die Argumente und startet den Durchlauf."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8787")
    arguments = parser.parse_args()
    return asyncio.run(run(arguments.base_url.rstrip("/")))


if __name__ == "__main__":
    sys.exit(main())
