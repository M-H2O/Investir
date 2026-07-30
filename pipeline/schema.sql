-- =====================================================================
--  Boussole — socle de données pour le simulateur d'allocation
--  Cible : SQLite (tests). Voir les notes « Postgres » pour la migration.
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
--  1. instruments — dimension. Change rarement.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id   INTEGER PRIMARY KEY,

    -- Ticker d'affichage, celui que l'utilisateur reconnaît (CSPX, IWDA…).
    ticker          TEXT    NOT NULL,

    -- Symbole de RÉCUPÉRATION chez Yahoo. Volontairement distinct de `ticker` :
    -- les deux coïncident rarement (CSPX -> SXR8.DE, PE500 -> PSP5.PA,
    -- AGGH -> 0GGH.L). Confondre les deux, c'est interroger le mauvais
    -- instrument — ou aucun — sans que rien ne le signale.
    yahoo_symbol    TEXT    UNIQUE,

    isin            TEXT,
    name            TEXT    NOT NULL,
    asset_type      TEXT    NOT NULL CHECK (asset_type IN ('stock', 'etf')),
    currency        TEXT    NOT NULL,          -- ISO 4217, devise de cotation
    exchange        TEXT,                      -- place de cotation
    is_active       INTEGER NOT NULL DEFAULT 1,

    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),

    -- Un même ISIN peut coter sur plusieurs places sous des tickers différents :
    -- l'unicité porte sur le couple, jamais sur le ticker seul.
    UNIQUE (ticker, exchange)
);

-- ---------------------------------------------------------------------
--  2. prices_daily — le fait. Une ligne par instrument et par jour.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prices_daily (
    instrument_id   INTEGER NOT NULL REFERENCES instruments(instrument_id),
    price_date      TEXT    NOT NULL,          -- 'YYYY-MM-DD'

    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,                      -- brut, tel que coté ce jour-là
    adjusted_close  REAL,                      -- ajusté splits + dividendes
    volume          INTEGER,

    -- Traçabilité : d'où vient la ligne, et de quand date sa récupération.
    -- Sans ça, une donnée fausse est indiscernable d'une donnée à jour.
    source          TEXT    NOT NULL,
    ingested_at     TEXT    NOT NULL DEFAULT (datetime('now')),

    PRIMARY KEY (instrument_id, price_date)
);

-- Accès dominant : « historique d'un instrument sur une période » — servi par
-- la PK. Second accès : « tous les instruments à une date » (valorisation d'un
-- portefeuille à date), qui a besoin de son propre index.
CREATE INDEX IF NOT EXISTS idx_prices_daily_date
    ON prices_daily (price_date, instrument_id);

-- ---------------------------------------------------------------------
--  3. fx_rates — conversion à la LECTURE, jamais à l'ingestion.
-- ---------------------------------------------------------------------
-- Les 20 ETF du comparateur ont tous une cotation en euros, donc cette table
-- reste vide pour eux. Elle devient indispensable dès l'ajout d'actions
-- américaines : convertir et écraser au stockage rendrait le choix de devise
-- irréversible et interdirait de comparer en USD et en EUR.
CREATE TABLE IF NOT EXISTS fx_rates (
    currency_pair   TEXT    NOT NULL,          -- ex. 'EUR/USD'
    rate_date       TEXT    NOT NULL,
    rate            REAL    NOT NULL,          -- 1 unité de base = `rate` en cotée
    source          TEXT    NOT NULL,
    ingested_at     TEXT    NOT NULL DEFAULT (datetime('now')),

    PRIMARY KEY (currency_pair, rate_date)
);

-- ---------------------------------------------------------------------
--  4. prices_weekly — VUE, pas table.
-- ---------------------------------------------------------------------
-- Une barre hebdomadaire est entièrement calculable depuis le quotidien. La
-- stocker en dur créerait une seconde source de vérité qui peut diverger de la
-- première (correction appliquée en daily, oubliée en weekly).
-- À ne convertir en table que si un fournisseur livre un jour de l'hebdo NATIF.
CREATE VIEW IF NOT EXISTS prices_weekly AS
WITH borne AS (
    SELECT
        instrument_id,
        price_date,
        -- lundi de la semaine : on recule de (jour de semaine - 1) jours,
        -- strftime('%w') renvoyant 0 pour dimanche.
        date(price_date, '-' || ((strftime('%w', price_date) + 6) % 7) || ' days')
            AS week_start,
        open, high, low, close, adjusted_close, volume,
        ROW_NUMBER() OVER (
            PARTITION BY instrument_id,
                         date(price_date, '-' || ((strftime('%w', price_date) + 6) % 7) || ' days')
            ORDER BY price_date
        ) AS rang_debut,
        ROW_NUMBER() OVER (
            PARTITION BY instrument_id,
                         date(price_date, '-' || ((strftime('%w', price_date) + 6) % 7) || ' days')
            ORDER BY price_date DESC
        ) AS rang_fin
    FROM prices_daily
)
SELECT
    instrument_id,
    week_start,
    MAX(CASE WHEN rang_debut = 1 THEN open END)           AS open,
    MAX(high)                                             AS high,
    MIN(low)                                              AS low,
    MAX(CASE WHEN rang_fin  = 1 THEN close END)           AS close,
    MAX(CASE WHEN rang_fin  = 1 THEN adjusted_close END)  AS adjusted_close,
    SUM(volume)                                           AS volume,
    COUNT(*)                                              AS trading_days
FROM borne
GROUP BY instrument_id, week_start;

-- ---------------------------------------------------------------------
--  Notes de migration Postgres
-- ---------------------------------------------------------------------
--   * REAL -> NUMERIC(18,6). SQLite n'a pas de décimal : REAL (float64) suffit
--     pour une simulation, mais sur des montants réels le flottant dérive.
--   * TEXT 'YYYY-MM-DD' -> DATE ; datetime('now') -> now().
--   * prices_daily gagne à être partitionnée par année au-delà de ~50 M lignes.
