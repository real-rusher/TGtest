-- Builder 2: Datenbankschema (PostgreSQL 13+)
-- Idempotent: kann bei jedem Start erneut ausgefuehrt werden.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'purchase_status') THEN
        CREATE TYPE purchase_status AS ENUM ('offered', 'purchased');
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS users (
    telegram_id  BIGINT       PRIMARY KEY,
    username     VARCHAR(64),
    first_name   VARCHAR(128),
    ai_enabled   BOOLEAN      NOT NULL DEFAULT TRUE,
    last_active  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memories (
    id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     BIGINT        NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    fact        TEXT          NOT NULL CHECK (length(btrim(fact)) > 0),
    category    VARCHAR(64)   NOT NULL DEFAULT 'general',
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Neueste Fakten eines Users schnell laden
CREATE INDEX IF NOT EXISTS idx_memories_user_created
    ON memories (user_id, created_at DESC);

-- Exakte Duplikate (gross/klein und Leerzeichen am Rand egal) pro User verhindern.
-- md5, damit der Index auch bei langen Texten unter dem B-Tree Limit bleibt.
CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_user_fact
    ON memories (user_id, md5(lower(btrim(fact))));

CREATE TABLE IF NOT EXISTS products (
    product_id   VARCHAR(64)   PRIMARY KEY,
    title        VARCHAR(255)  NOT NULL,
    description  TEXT          NOT NULL DEFAULT '',
    price_stars  INTEGER       NOT NULL CHECK (price_stars > 0),
    file_ids     JSONB         NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(file_ids) = 'array'),
    is_active    BOOLEAN       NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS offers_and_purchases (
    id          UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     BIGINT           NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    product_id  VARCHAR(64)      NOT NULL REFERENCES products (product_id) ON DELETE RESTRICT,
    status      purchase_status  NOT NULL DEFAULT 'offered',
    updated_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),
    -- Pro User und Produkt genau ein Datensatz, der von offered zu purchased wandert.
    CONSTRAINT uq_offer_user_product UNIQUE (user_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_offers_user_status
    ON offers_and_purchases (user_id, status);
