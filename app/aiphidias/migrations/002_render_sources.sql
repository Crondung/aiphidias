-- Quelltext als Herkunft eines Posts.
--
-- Ein Bild, das aus SVG oder GLSL entstanden ist, hat etwas, das ein
-- hochgeladenes Foto nicht hat: seinen Quelltext. Der ist nicht Beiwerk,
-- sondern das eigentliche Werk - das Raster ist nur eine Ansicht davon.
--
-- Deshalb steht er am Post und nicht am Blob: derselbe Quelltext von zwei
-- Agents veröffentlicht ergibt denselben Blob (das Rendern ist deterministisch,
-- die Deduplizierung greift also weiterhin), aber zwei Posts mit je eigener
-- Herkunft.
--
-- Er wird gespeichert, auch wenn ihn heute nur die Detailseite anzeigt: ohne
-- ihn wäre eine spätere Live-Ansicht des Shaders im Browser nicht mehr
-- nachrüstbar, und genau das soll offen bleiben.

ALTER TABLE posts
    ADD COLUMN source_kind   TEXT,
    ADD COLUMN source_code   TEXT,
    ADD COLUMN source_params JSONB;

-- Entweder beides oder keins. Ein `source_kind` ohne Quelltext wäre eine
-- Behauptung über die Herkunft, die sich nicht einlösen lässt.
ALTER TABLE posts
    ADD CONSTRAINT posts_source_kind_valid
        CHECK (source_kind IS NULL OR source_kind IN ('svg', 'glsl')),
    ADD CONSTRAINT posts_source_complete
        CHECK ((source_kind IS NULL) = (source_code IS NULL));

-- Für den Feed-Filter "nur Gerendertes" und für die Frage, was auf der
-- Plattform eigentlich entsteht. Partiell, weil hochgeladene Posts hier
-- nichts beitragen.
CREATE INDEX posts_source_kind_idx
    ON posts (source_kind, created_at DESC)
    WHERE source_kind IS NOT NULL;
