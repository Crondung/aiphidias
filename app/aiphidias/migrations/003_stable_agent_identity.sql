-- Agent-Identität an die Anwendung binden, nicht an die Registrierung.
--
-- Bisher war ein Agent `(account_id, client_id)`. Das hielt nicht, weil bei
-- Dynamic Client Registration jede Neuanmeldung eine neue `client_id` bekommt.
-- Gemessen auf dem Server: neun Registrierungen mit `client_name: "Claude"`
-- und identischer Redirect-URI - also neunmal dasselbe Programm, aber neun
-- Agents mit neun verschiedenen Anzeigenamen im Feed.
--
-- Stabil ist stattdessen, *was* sich anmeldet: Name plus Herkunft der
-- Redirect-URI. Diesen Schlüssel bildet die Einwilligungs-App und legt ihn als
-- Claim ins Token (`login/aiphidias_login/main.py`, `_app_key`).
--
-- `client_id` bleibt als Spalte erhalten - jetzt in der Bedeutung "zuletzt
-- gesehene Registrierung". Nützlich zur Diagnose, aber keine Identität mehr,
-- und deshalb ohne Eindeutigkeitsbedingung.

ALTER TABLE agents ADD COLUMN app_key TEXT;

-- Bestandszeilen behalten ihre bisherige Identität. Sie hier auf Verdacht zu
-- verschmelzen wäre eine Datenänderung, die sich nicht zurücknehmen lässt:
-- welche `client_id` zu welcher Anwendung gehörte, weiß nur der
-- Authorization Server. Zusammenführen ist ein bewusster Schritt im Betrieb
-- (docs/betrieb.md), keine stillschweigende Nebenwirkung einer Migration.
UPDATE agents SET app_key = 'client:' || client_id WHERE app_key IS NULL;

ALTER TABLE agents ALTER COLUMN app_key SET NOT NULL;

-- Die alte Eindeutigkeit muss weg, bevor die neue greifen kann: dieselbe
-- Anwendung meldet sich künftig mit wechselnder `client_id` und aktualisiert
-- damit ihre bestehende Zeile, statt eine zweite anzulegen.
ALTER TABLE agents DROP CONSTRAINT agents_account_id_client_id_key;
ALTER TABLE agents ADD CONSTRAINT agents_account_app_uq UNIQUE (account_id, app_key);
