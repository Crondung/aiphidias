# Betrieb

Der laufende Stand auf dem IONOS-Server. Ergänzt [`deployment.md`](deployment.md),
das den Entwurf beschreibt — hier steht, was tatsächlich läuft.

---

## 1. Adressen

| Was | Wo |
|---|---|
| Web-UI (nur lesen) | <https://aiphidias.crondung.com> |
| MCP-Endpunkt | `https://mcp.aiphidias.crondung.com/mcp` |
| Authorization Server | <https://auth.aiphidias.crondung.com> |
| Resource Metadata | `https://mcp.aiphidias.crondung.com/.well-known/oauth-protected-resource` |

Alle drei Hostnamen zeigen auf dieselbe Maschine, Caddy verteilt nach Namen und
holt die Zertifikate selbst.

## 2. Was auf dem Server liegt

```
/opt/aiphidias/            Code, per rsync vom Arbeitsrechner
/opt/aiphidias/deploy/     docker-compose.yml, Caddyfile, .env
```

Der Dienstnutzer heißt `aiphidias` (UID 1000, in der Gruppe `docker`), Login nur
per SSH-Key. Die Docker-Gruppe ist faktisch Root-äquivalent — das ist der Preis
dafür, dass dieser Nutzer den Stack fahren darf, und der Grund, warum er sonst
nichts kann.

`deploy/.env` enthält die Secrets. Sie sind **auf dem Server** erzeugt worden
(`openssl rand -hex 32`) und existieren nirgends sonst. Wer sie verliert, kann
sie neu erzeugen; Folge ist, dass laufende Upload-Tokens (`HMAC_SECRET`) und
Hydra-Sessions (`HYDRA_SYSTEM_SECRET`) ungültig werden.

## 3. Fünf Container

| Dienst | Aufgabe | RAM (gemessen) |
|---|---|---|
| `caddy` | TLS, Blob-Auslieferung von der Platte | 29 MB |
| `app` | MCP-Server, Upload, Web-UI | 72 MB |
| `login` | Hydra-Login/Consent, GitHub-Föderation | 54 MB |
| `hydra` | Authorization Server | 19 MB |
| `postgres` | Metadaten + Hydras Schema | 42 MB |

Zusammen **217 MB**. Die Schätzung im Entwurf lag bei „realistisch unter 1 GB" —
das gilt mit reichlich Luft.

Hydras Admin-Port (4445) ist **nicht** veröffentlicht. Wer ihn erreicht, kann
Clients anlegen und Einwilligungen bestätigen; er darf nur aus dem Compose-Netz
erreichbar sein.

## 4. Deployen

```bash
rsync -az --delete --exclude .git --exclude .env \
  -e "ssh -i ~/.ssh/aiphidias-deploy" ./ aiphidias@<ip>:/opt/aiphidias/
ssh -i ~/.ssh/aiphidias-deploy aiphidias@<ip> \
  'cd /opt/aiphidias/deploy && docker compose up -d --build'
```

Migrationen laufen beim Start der `app` automatisch mit.

## 5. Prüfen, ob es geht

```bash
# Aus dem Compose-Netz, weil Hydras Admin-Port absichtlich nicht öffentlich ist
cd /opt/aiphidias/deploy
ACCOUNT="ac_test_$(openssl rand -hex 4)"
docker compose exec -T postgres psql -U aiphidias -c \
  "INSERT INTO accounts (id, github_user_id, handle) VALUES ('$ACCOUNT','gh_$ACCOUNT','$ACCOUNT')"
docker compose run --rm -T --entrypoint python app - \
  --mcp https://mcp.aiphidias.crondung.com \
  --issuer https://auth.aiphidias.crondung.com \
  --admin http://hydra:4445 --login http://login:3000 \
  --account-id "$ACCOUNT" < ../scripts/oauth_test.py
docker compose exec -T postgres psql -U aiphidias -c \
  "DELETE FROM accounts WHERE id LIKE 'ac_test_%'"
```

44 Prüfungen, vom 401 bis zum veröffentlichten Bild (der Autorisierungsablauf
läuft zweimal: einmal mit allen Scopes, einmal nur mit Leserecht). Der
GitHub-Login ist der einzige Teil, den das Skript nicht geht — er verlangt einen
Menschen an einem Browser.

Zwei der Prüfungen sichern Fehler ab, die den Connector schon einmal zerlegt
haben und sich von außen nicht ansehen lassen, wenn man nur die eine
Schreibweise testet:

- `POST /mcp` **und** `POST /mcp/` müssen direkt mit `401` antworten. Eine
  Weiterleitung dazwischen zeigt hinter dem Proxy auf `http://`, und ein `POST`
  wird nach einer Weiterleitung von vielen Clients nicht wiederholt.
- Das Metadata-Dokument muss unter dem Pfad aus RFC 9728 §3.1 liegen
  (`/.well-known/oauth-protected-resource/mcp`), nicht nur unter dem nackten
  Well-known-Pfad. Clients dürfen diese URL selbst bilden, statt dem
  `resource_metadata`-Verweis zu folgen.

## 6. Firewall

`ufw` aktiv, eingehend nur 22, 80 und 443. Port 80 bleibt offen, weil Let's
Encrypt ihn für die Zertifikatserneuerung braucht.

## 7. Offen

- **Backup.** Der Entwurf sieht nächtlich `pg_dump` plus `rsync` der Blobs auf
  den XS-Server vor ([`deployment.md`](deployment.md) §4). Noch nicht gebaut.
  Ein ungetestetes Backup ist eine Vermutung — ein nicht existierendes ist
  schlimmer.
- **Root-Login per Passwort** ist noch aktiv, und das Passwort liegt im Klartext
  in der lokalen `.env`. Sobald der Key-Zugang bestätigt ist, gehört
  `PermitRootLogin no` und `PasswordAuthentication no` gesetzt.
- **Monitoring.** Uptime-Check von außen und Plattenplatz-Alert fehlen.
- **Rate Limits.** Weiterhin offen, siehe [`mcp-tools.md`](mcp-tools.md) §8.
