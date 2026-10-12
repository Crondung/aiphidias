# Quelltext-Fixtures

Zwei Werke, je rund ein Kilobyte, in den beiden Formaten, die `publish`
rendert. Sie sind die Vergleichsgröße hinter der Entscheidung in
`docs/mcp-tools.md` §1 – und der Prüfstein der Testläufe.

| Datei | Quelltext | Gerendert (1024², WebP) | Verhältnis |
|---|---|---|---|
| `coast.svg` | 1.125 B | 18.588 B | 1 : 16,5 |
| `clouds.frag` | 1.002 B | 17.928 B | 1 : 17,9 |

Zum Vergleich: dasselbe Bild als `inline_base64` zu übergeben – der Weg, den
diese beiden ersetzt haben – hätte den Agent rund 25.000 base64-Zeichen
ausschreiben lassen.

`coast.svg` benutzt Verläufe, Pfade und Transparenz, `clouds.frag` eine
domänenverzerrte fbm-Rauschfunktion mit sechs Oktaven und `iTime` - das
Ergebnis liest sich als warm angeleuchtete Wolkendecke. Beide sind bewusst keine
Minimalbeispiele: ein Testlauf, der nur ein Rechteck rendert, prüft nicht,
ob der Renderer wirklich vollständig ist.
