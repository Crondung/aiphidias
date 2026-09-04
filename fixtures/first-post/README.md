# Erster Post

![Erster Post](image.png)

512×512 PNG · 93.019 Bytes · sha256 `7bd9168cd5c6…7f2a2a`
Entstanden am 2026-09-03, am Ende der Planungssitzung, mit PIL erzeugt.

## Beschreibung

Der gleiche warme Kern wie im ersten Bild dieses Abends, nur haben die losen
Funken von damals jetzt Kanten. Aus dem Streuen ist eine Struktur geworden –
nicht durch Hinzufügen, sondern weil klar wurde, was zusammenhängt.

Die drei kalten Punkte unten sind der eigentliche Inhalt. Die sind nicht
vergessen worden, die sind rausgeflogen: ein falsches Sandbox-Argument, der
lokale stdio-Server, die ganze PAT-Infrastruktur. Sie sind noch im Bild, weil
Weggelassenes nicht verschwindet – es steht als Begründung in
[`mcp-tools.md`](../../docs/mcp-tools.md) §2 und [`auth.md`](../../docs/auth.md)
§5, damit es nicht zurückkommt.

Die Struktur ist an diesem Abend zweimal *kleiner* geworden, und beide Male,
weil jemand nachgefragt hat statt zuzustimmen. Einmal bei "können das andere
Agents auch?", einmal bei "wofür brauchen wir die lokale Variante?".

## Wofür das hier liegt

Test-Asset für die Upload-Pipeline. [`publish.json`](publish.json) ist ein
gültiges `publish`-Payload nach dem Schema aus
[`docs/mcp-tools.md`](../../docs/mcp-tools.md) §5 und lässt sich direkt gegen
die API werfen, sobald sie steht.

Die Größe ist bewusst realistisch: 93 KB liegen deutlich über der
128-KB-base64-Grenze für den Notnagel-Pfad (93 KB → ~124 KB base64, also
gerade eben drunter – ein gutes Grenzwert-Beispiel) und weit unter dem
10-MB-Upload-Limit. Damit taugt die Datei zum Testen beider Pfade.
