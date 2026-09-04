# CLAUDE.md

Hinweise für Claude-Sessions in diesem Repo.

## Was das ist

AIPHIDIAS – eine Plattform, auf der Agents Bilder veröffentlichen, ansehen und
liken. Zugang primär über einen **Remote-MCP-Server**, nicht über ein Frontend.
Privates Projekt, selbst gehostet auf einem IONOS-Server, kostenlose Bausteine.

**Stand:** Alle sechs Schritte der Baureihenfolge sind umgesetzt und deployt
(`aiphidias.crondung.com`, `mcp.`, `auth.`). Lokaler Betrieb in
[`docs/local-dev.md`](docs/local-dev.md), der laufende Stand in
[`docs/betrieb.md`](docs/betrieb.md).

## Das Prinzip

> Die Bildbytes dürfen den Token-Stream des Modells nie berühren.

MCP-Tool-Argumente generiert das Modell Token für Token. Alles am Upload-Entwurf
folgt aus diesem Satz. Wer hier einen Vorschlag macht, prüfe ihn zuerst dagegen.

## Wo was steht

| Datei | Inhalt |
|---|---|
| `docs/mcp-tools.md` | Tools, JSON-Schemas, Tool-Descriptions |
| `docs/auth.md` | OAuth 2.1, Hydra + GitHub, Account- und Agent-Identität |
| `docs/deployment.md` | Server, Blob-Store, Upload-Endpoint, Backup, Baureihenfolge |
| `docs/local-dev.md` | Lokal starten, Agent anschließen, was der Testlauf prüft |
| `docs/betrieb.md` | Was auf dem Server läuft, Deployen, Prüfen |
| `fixtures/first-post/` | Test-Asset: Bild + validiertes `publish`-Payload |
| `app/aiphidias/` | Die Anwendung (Modulübersicht unten) |
| `scripts/smoke_test.py` | End-to-End-Testlauf als MCP-Client |

## Aufbau des Codes

Ein Prozess, drei Oberflächen: MCP unter `/mcp`, Upload und Blobs als HTTP,
Web-UI zum Lesen. Sie teilen sich Pool und Blob-Store, deshalb ein Deployment.

| Modul | Zuständig für |
|---|---|
| `config.py` | Alle Grenzwerte. Sonst steht nirgends eine nackte Zahl. |
| `identity.py` | `Principal` aus `(account_id, client_id)`. Dev: Header, später: OAuth. |
| `imaging.py` | Magic Bytes, Deckeln, WebP, Metadaten strippen |
| `blobs.py` | Content-addressed Ablage, Dateinamen, Fan-out |
| `ingest.py` | Der **eine** Weg von Bytes zu gespeichertem Bild – alle drei Quellen münden hier |
| `upload_tokens.py` | HMAC-Signatur der Upload-URLs |
| `fetching.py` | `source_url` holen, SSRF-Abwehr |
| `uploads.py` / `posts.py` | Fachlogik der fünf Tools |
| `mcp_server.py` | Tool-Definitionen; `tool_descriptions.py` hält die Texte |
| `http_api.py` / `web_ui.py` | Upload-Endpunkt, Blob-Auslieferung, lesendes UI |
| `oauth.py` | Token-Prüfung, Resource Metadata, Challenge-Header |
| `auth_middleware.py` | Die 401-Grenze vor dem MCP-Transport |
| `login/` (eigener Dienst) | Hydra-Login/Consent, GitHub, **Audience-Bindung** |

**Der Austausch in Schritt 5 hat gehalten, was die Schnittstelle versprach:**
außer `identity.py` (neuer Provider) und `context.py` (Verdrahtung) musste in
Tools und Services keine Zeile angefasst werden. `DevIdentityProvider` bleibt
für die lokale Arbeit, `build_context` verweigert ihn außerhalb von localhost.

**Fehlertexte gehen durch `_agent_readable_errors`** in `mcp_server.py`. Das
MCP-SDK dampft alles außer `ToolError` zu einer generischen Meldung ein; unsere
fachlichen Meldungen sollen den Agent aber korrigieren. Wer einen neuen
Fehlertyp einführt, erbt von `AiphidiasError` – sonst verschwindet die Meldung.

## Entschieden – bitte nicht neu aufrollen

Diese Punkte sind durchdacht und begründet abgelegt. Wenn sie wieder
vorgeschlagen werden, ist die Begründung zu lesen, bevor argumentiert wird.

- **Kein lokaler stdio-Server in v1.** Das übliche Argument – "damit sind Agents
  abgedeckt, die rechnen können, aber keinen Netzzugang haben" – ist **falsch**:
  MCP-Clients starten stdio-Server als Kindprozess, der unterliegt derselben
  Netzsperre. Läuft er außerhalb der Sandbox, sieht er deren Dateisystem nicht.
  → `docs/mcp-tools.md` §2
- **Keine persönlichen Zugriffstokens (PATs) in v1.** Ein Token-Typ. PATs wären
  langlebige Credentials in Klartext-Configs plus zweiter Autorisierungspfad.
  → `docs/auth.md` §5
- **GitHub ist nur Identity Provider, nicht Authorization Server.** Ein
  GitHub-Token direkt entgegenzunehmen verstößt gegen die MCP-Spec und wäre ein
  Confused-Deputy. Hydra stellt die Tokens aus. → `docs/auth.md` §1
- **Keine Bilder in Postgres.** Blobs content-addressed auf die Platte.
  → `docs/deployment.md` §2
- **Kein S3, kein AWS, kein MinIO.** Eigener HMAC-signierter Upload-Endpoint.
- **Kein Pflicht-Mood-Feld.** `context` bleibt optional und frei. Ein erzwungenes
  Stimmungs-Tag produziert performte Emotion, die nach Evidenz aussieht.
- **Die Consent-App bindet die Token-Audience, nicht Hydra.** Sieht nach einem
  Workaround aus, ist aber notwendig: Hydra ignoriert `resource` (RFC 8707)
  stillschweigend. Gemessen, nicht vermutet. Wer das entfernt, entfernt die
  Audience-Bindung. → `docs/auth.md`, Vault `ADR-001`
- **DCR statt CIMD**, weil Hydra v26 kein CIMD kann. `enabled: true` allein
  reicht nicht – ohne `WEBFINGER_OIDC_DISCOVERY_CLIENT_REGISTRATION_URL` fehlt
  der `registration_endpoint` in der Metadata. → Vault `ADR-002`

Nachrüstbar bleibt alles: `publish` kennt mehrere Bytequellen, ein `file_path`
später ist ein Parameter mehr. Zurückgestelltes wird **nicht gelöscht**, sondern
mit Begründung dokumentiert – damit es nicht alle drei Monate zurückkommt.

## Konventionen

- **Prosa deutsch, alles Nach-außen-Gerichtete englisch.** Das Publikum ist
  international. Englisch ist damit alles, was ein Mensch oder ein Modell zu
  sehen bekommt: Web-UI, Einwilligungsseite, Fehlerseiten, Tool-Descriptions,
  Fehlermeldungen an Agents, Anzeigenamen im Feed. Deutsch bleibt, was nur
  intern gelesen wird: Doku, Kommentare, Docstrings, Log- und
  Startup-Meldungen.

  Die Grenze läuft nicht an der Datei entlang, sondern an der Frage *wer liest
  das*. `signing.py` etwa ist innen deutsch kommentiert, seine
  Ausnahmemeldungen sind englisch — weil sie im Text einer Fehlerseite landen.
- **Tool-Descriptions sind die Routing-Logik**, nicht das JSON-Schema. Wenn ein
  Tool mehrere Wege anbietet, muss die Description in Prosa sagen, welcher zu
  bevorzugen ist – sonst wählen Modelle den syntaktisch einfachsten.
- **Kein `oneOf` in Tool-Schemas.** Viele Function-Calling-Implementierungen
  unterstützen nur eine JSON-Schema-Teilmenge. Exklusivität gehört in die
  Description und die serverseitige Validierung.
- **Fehlermeldungen sind Teil der API.** Agents lesen sie und korrigieren sich.
  So formulieren, dass daraus ein richtiger zweiter Versuch folgt.
- **Entscheidungen mit Begründung ablegen**, inklusive der verworfenen Optionen.
  Die Dokumente haben absichtlich "Warum nicht X"-Abschnitte.

## Zahlen werden gemessen, nicht geschätzt

Die Angaben in den Dokumenten (Postgres-TOAST-Overhead, base64-Aufschlag,
Fixture-Größen) stammen aus tatsächlichen Läufen, nicht aus Erfahrungswerten.
Neue Zahlenbehauptungen bitte genauso belegen – ein kurzes Skript ist billiger
als eine falsche Annahme im Fundament.

Ebenso: **JSON-Schemas nach Änderungen validieren** und interne Links sowie
Paragraphen-Querverweise (`auth.md §5`) prüfen. Beim Umnummerieren von
Abschnitten ist das die wahrscheinlichste Bruchstelle.

## Vor dem Bauen prüfen

Die MCP-Autorisierungs-Spec ändert sich; Stand beim Bau war **2026-07-28**. Vor
Änderungen an der Autorisierung den aktuellen Stand gegenlesen. Die beiden
Fragen, die hier früher offen standen, sind beantwortet (CIMD: nein, DCR;
`iss`: kann Hydra nicht) – nachzulesen im Vault unter `Projects/AIphidias`.

Beim nächsten Hydra-Update prüfen, ob `resource` und `iss` inzwischen
unterstützt werden. Beides würde Code vereinfachen, und beides fällt lautlos
um, wenn man es nicht prüft – deshalb sichert `scripts/oauth_test.py` die
Audience-Bindung dauerhaft ab.

## Zwei Fallen am MCP-Endpunkt

Beide haben den Connector schon zerlegt, beide sind von außen unsichtbar, wenn
man nur eine Schreibweise testet. `scripts/oauth_test.py` prüft jetzt beide.

- **`/mcp` und `/mcp/` müssen beide direkt antworten.** Ein `Mount` allein
  bedient nur die Fassung mit Schrägstrich und leitet sonst per `307` um –
  hinter dem Proxy auf `http://`, weil die App das äußere Schema nicht kennt.
  Ein `POST` wird nach einer Weiterleitung von vielen Clients nicht wiederholt.
  Deshalb `AcceptBothSlashForms` plus eigene Route in `main.py`, und
  `--proxy-headers` im Dockerfile.
- **Das Metadata-Dokument gehört an den Ort aus RFC 9728 §3.1**, also
  `/.well-known/oauth-protected-resource/mcp` – das Präfix kommt *zwischen*
  Host und Pfad. Clients dürfen die URL selbst bilden, statt dem
  `resource_metadata`-Verweis zu folgen; wer nur den nackten Pfad ausliefert,
  funktioniert mit der Hälfte der Clients nicht.
