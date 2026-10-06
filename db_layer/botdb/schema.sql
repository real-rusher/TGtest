-- Datenbankschema (PostgreSQL 13+)
-- Idempotent: kann bei jedem Start erneut ausgefuehrt werden.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status') THEN
        CREATE TYPE payment_status AS ENUM ('pending', 'paid');
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS users (
    telegram_id   BIGINT       PRIMARY KEY,
    username      VARCHAR(64),
    first_name    VARCHAR(128),
    ai_enabled    BOOLEAN      NOT NULL DEFAULT TRUE,   -- false nach /stop des Users
    credits       INTEGER      NOT NULL DEFAULT 0 CHECK (credits >= 0),  -- verbleibende KI-Antworten
    disclosed_at  TIMESTAMPTZ,                          -- wann der KI-Hinweis verschickt wurde
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_active   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memories (
    id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     BIGINT        NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    fact        TEXT          NOT NULL CHECK (length(btrim(fact)) > 0),
    category    VARCHAR(64)   NOT NULL DEFAULT 'general',
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memories_user_created
    ON memories (user_id, created_at DESC);

-- Exakte Duplikate (gross/klein und Leerzeichen am Rand egal) pro User verhindern.
-- md5, damit der Index auch bei langen Texten unter dem B-Tree Limit bleibt.
CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_user_fact
    ON memories (user_id, md5(lower(btrim(fact))));

-- Chatverlauf fuer den Antwort-Generator und die Fakten-Extraktion
CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL     PRIMARY KEY,
    user_id     BIGINT        NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    role        VARCHAR(16)   NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT          NOT NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_user_id
    ON messages (user_id, id DESC);

-- Kaufbare Pakete: jedes Paket schreibt `credits` KI-Antworten gut
CREATE TABLE IF NOT EXISTS products (
    product_id   VARCHAR(64)   PRIMARY KEY,
    title        VARCHAR(255)  NOT NULL,
    description  TEXT          NOT NULL DEFAULT '',
    price_cents  INTEGER       NOT NULL CHECK (price_cents > 0),
    currency     CHAR(3)       NOT NULL DEFAULT 'EUR',
    credits      INTEGER       NOT NULL CHECK (credits > 0),
    is_active    BOOLEAN       NOT NULL DEFAULT TRUE
);

-- Protokoll, wann wem welches Paket angeboten wurde (fuer den Angebots-Cooldown)
CREATE TABLE IF NOT EXISTS offers (
    id          BIGSERIAL     PRIMARY KEY,
    user_id     BIGINT        NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    product_id  VARCHAR(64)   NOT NULL REFERENCES products (product_id) ON DELETE RESTRICT,
    offered_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_offers_user_time
    ON offers (user_id, offered_at DESC);

-- Zahlungen ueber externe Zahlungslinks.
-- user_id wird beim Loeschen eines Users auf NULL gesetzt: Zahlungsbelege muessen
-- aus steuerlichen Gruenden erhalten bleiben, der Personenbezug nicht.
CREATE TABLE IF NOT EXISTS payments (
    id            UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    provider      VARCHAR(32)     NOT NULL,
    provider_ref  VARCHAR(255)    NOT NULL,
    user_id       BIGINT          REFERENCES users (telegram_id) ON DELETE SET NULL,
    product_id    VARCHAR(64)     NOT NULL REFERENCES products (product_id) ON DELETE RESTRICT,
    amount_cents  INTEGER         NOT NULL CHECK (amount_cents > 0),
    currency      CHAR(3)         NOT NULL,
    credits       INTEGER         NOT NULL CHECK (credits > 0),
    status        payment_status  NOT NULL DEFAULT 'pending',
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT now(),
    paid_at       TIMESTAMPTZ,
    CONSTRAINT uq_payment_provider_ref UNIQUE (provider, provider_ref)
);

CREATE INDEX IF NOT EXISTS idx_payments_user_status
    ON payments (user_id, status);
