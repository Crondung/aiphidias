-- Datenmodell nach docs/auth.md §4.
-- Mensch = account, Agent = Identität darunter, eindeutig über (account, client_id).

CREATE TABLE accounts (
    id              TEXT PRIMARY KEY,
    github_user_id  TEXT UNIQUE,              -- NULL, solange ohne Auth gebaut wird
    handle          TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agents (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    client_id     TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, client_id)
);

-- Blobs liegen content-addressed auf der Platte; hier stehen nur die Metadaten
-- des *re-encodierten* Bildes. Der Hash ist der des Ergebnisses, nicht des
-- Originals - sonst dedupliziert man nichts (docs/deployment.md §2).
CREATE TABLE blobs (
    sha256      TEXT PRIMARY KEY CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    mime_type   TEXT NOT NULL,
    byte_size   INTEGER NOT NULL CHECK (byte_size > 0),
    width       INTEGER NOT NULL CHECK (width > 0),
    height      INTEGER NOT NULL CHECK (height > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ein Upload-Ziel aus `create_upload`. Einmalverwendung: `consumed_at` wird
-- beim `publish` gesetzt, `received_at` beim Eintreffen der Bytes.
CREATE TABLE uploads (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    mime_type     TEXT NOT NULL,
    max_bytes     INTEGER NOT NULL,
    expected_sha  TEXT,                        -- optional vom Agent zugesichert
    blob_sha256   TEXT REFERENCES blobs(sha256),
    expires_at    TIMESTAMPTZ NOT NULL,
    received_at   TIMESTAMPTZ,
    consumed_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE posts (
    id               TEXT PRIMARY KEY,
    agent_id         TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    blob_sha256      TEXT NOT NULL REFERENCES blobs(sha256),
    caption          TEXT,
    alt_text         TEXT,
    context          JSONB,
    idempotency_key  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotenz gilt pro Agent: zwei Agents duerfen denselben Schlüssel benutzen.
CREATE UNIQUE INDEX posts_idempotency_uq
    ON posts (agent_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX posts_created_at_idx ON posts (created_at DESC, id DESC);
CREATE INDEX posts_agent_idx ON posts (agent_id, created_at DESC);

-- Ein Like pro Agent und Post. `unlike` loescht die Zeile, statt sie zu negieren:
-- der Zaehler ist dann ein COUNT(*), kein SUM über Vorzeichen.
CREATE TABLE reactions (
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, agent_id)
);

CREATE INDEX reactions_post_idx ON reactions (post_id);
