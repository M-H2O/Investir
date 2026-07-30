"""Tests du moteur de calcul de portefeuille (pipeline/portefeuille.py).

Deux familles de tests, volontairement séparées :

  * Les tests sur base SYNTHÉTIQUE (la majorité) — des prix ronds, choisis pour
    que chaque valeur attendue se recalcule de tête. Ils ne dépendent ni du
    réseau ni de l'état réel de boussole.db, donc ils restent reproductibles
    indéfiniment.

  * Un test d'INTÉGRATION sur la vraie base (`test_reference_donnees_reelles`)
    — IWDA/CSPX/MEUD, 30/30/40, sur une fenêtre historique fixe. Les valeurs
    attendues ont été extraites de boussole.db le 2026-07-30 par une requête
    SQL directe (voir le commentaire au-dessus de la fonction), pas inventées
    ni recalculées autrement que par ce même calcul à la main. Si l'historique
    2024-03-01 -> 2025-01-02 de ces trois ETF venait à être corrigé par Yahoo
    après coup, ce test — et lui seul — cesserait de passer ; il est ignoré
    si boussole.db n'existe pas encore (avant un premier `ingest.py --full`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portefeuille import (
    InsufficientHistory,
    InvalidWeights,
    UnknownTicker,
    simulate,
)

RACINE = Path(__file__).parent.parent
SCHEMA = RACINE / "schema.sql"
DB_REELLE = RACINE / "boussole.db"


# ---------------------------------------------------------------------
#  Base synthétique — prix ronds, calculables à la main
# ---------------------------------------------------------------------
#
#   Ticker  2024-01-02   2024-01-03   2024-01-04
#   A          100.00       110.00       105.00
#   B           50.00        50.00        60.00
#   C          200.00      (aucune)      220.00     <- jour férié simulé
#   D             —            —        300.00      <- n'existe que depuis le 04
#
# Allocation de référence : A 50 % / B 30 % / C 20 %, capital 10 000.
#
#   capital investi : A=5000  B=3000  C=2000
#
#   02/01 (départ)  : A=5000.00  B=3000.00  C=2000.00           total=10000.00
#   03/01           : A=5500.00  B=3000.00  C=2000.00 (reporté) total=10500.00
#   04/01 (fin)      : A=5250.00  B=3600.00  C=2200.00           total=11050.00
#
#   gain_absolu = 1050.00   gain_pct = 10.50 %
#   detail : A prix 100->105 (+5%) · B prix 50->60 (+20%) · C prix 200->220 (+10%)

PRIX_SYNTHETIQUES = {
    "A": {"2024-01-02": 100.0, "2024-01-03": 110.0, "2024-01-04": 105.0},
    "B": {"2024-01-02": 50.0, "2024-01-03": 50.0, "2024-01-04": 60.0},
    "C": {"2024-01-02": 200.0, "2024-01-04": 220.0},
    "D": {"2024-01-04": 300.0},
}


@pytest.fixture()
def cx():
    connexion = sqlite3.connect(":memory:")
    connexion.row_factory = sqlite3.Row
    connexion.executescript(SCHEMA.read_text(encoding="utf-8"))

    for ticker, prix in PRIX_SYNTHETIQUES.items():
        connexion.execute(
            """INSERT INTO instruments (ticker, name, asset_type, currency, exchange)
               VALUES (?, ?, 'etf', 'EUR', 'TEST')""",
            (ticker, f"Instrument test {ticker}"),
        )
        instrument_id = connexion.execute(
            "SELECT instrument_id FROM instruments WHERE ticker = ?", (ticker,)
        ).fetchone()["instrument_id"]
        for date_, close in prix.items():
            connexion.execute(
                """INSERT INTO prices_daily
                       (instrument_id, price_date, close, adjusted_close, source)
                   VALUES (?, ?, ?, ?, 'test')""",
                (instrument_id, date_, close, close),
            )
    connexion.commit()
    yield connexion
    connexion.close()


ALLOC_REF = {"A": 50.0, "B": 30.0, "C": 20.0}


# ---------------------------------------------------------------------
#  Valeurs de référence — cas nominal
# ---------------------------------------------------------------------
def test_final_value_and_gain(cx):
    r = simulate(cx, ALLOC_REF, 10_000.0, "2024-01-02", "2024-01-04")

    assert r.start_date == "2024-01-02"
    assert r.end_date == "2024-01-04"
    assert r.initial_capital == pytest.approx(10_000.0)
    assert r.final_value == pytest.approx(11_050.0)
    assert r.gain_absolute == pytest.approx(1_050.0)
    assert r.gain_pct == pytest.approx(10.5)


def test_per_holding_breakdown(cx):
    r = simulate(cx, ALLOC_REF, 10_000.0, "2024-01-02", "2024-01-04")
    holdings = {d.ticker: d for d in r.holdings}

    assert holdings["A"].start_price == pytest.approx(100.0)
    assert holdings["A"].end_price == pytest.approx(105.0)
    assert holdings["A"].invested == pytest.approx(5_000.0)
    assert holdings["A"].final_value == pytest.approx(5_250.0)
    assert holdings["A"].gain_pct == pytest.approx(5.0)

    assert holdings["B"].final_value == pytest.approx(3_600.0)
    assert holdings["B"].gain_pct == pytest.approx(20.0)

    assert holdings["C"].final_value == pytest.approx(2_200.0)
    assert holdings["C"].gain_pct == pytest.approx(10.0)


def test_series_carries_last_known_price_over_holidays(cx):
    """C n'a pas de cotation le 03/01 : sa valeur ce jour-là doit être reportée
    depuis le 02/01 (200.00), pas absente ni à zéro — c'est le comportement que
    consommerait un curseur de date dans l'interface."""
    r = simulate(cx, ALLOC_REF, 10_000.0, "2024-01-02", "2024-01-04")
    values = dict(r.series)

    assert list(values.keys()) == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert values["2024-01-02"] == pytest.approx(10_000.0)
    assert values["2024-01-03"] == pytest.approx(10_500.0)
    assert values["2024-01-04"] == pytest.approx(11_050.0)


def test_single_holding_tracks_its_own_price(cx):
    r = simulate(cx, {"A": 100.0}, 1_000.0, "2024-01-02", "2024-01-04")
    # 100% sur une seule ligne : la valeur suit exactement le ratio de prix.
    assert r.final_value == pytest.approx(1_000.0 * 105.0 / 100.0)
    assert r.gain_pct == pytest.approx(5.0)


def test_default_end_date_is_last_common_day(cx):
    r = simulate(cx, ALLOC_REF, 10_000.0, "2024-01-02")
    assert r.end_date == "2024-01-04"


# ---------------------------------------------------------------------
#  Erreurs — doivent être explicites, jamais un résultat silencieusement faux
# ---------------------------------------------------------------------
def test_weights_not_summing_to_100_raise(cx):
    with pytest.raises(InvalidWeights):
        simulate(cx, {"A": 30.0, "B": 30.0, "C": 30.0}, 10_000.0, "2024-01-02")


def test_float_rounding_tolerance_accepted(cx):
    # 33.33 + 33.33 + 33.34 = 100.00 pile, mais un budget UI en float peut
    # produire 99.999999... à l'epsilon près : la tolérance doit l'absorber.
    r = simulate(cx, {"A": 33.34, "B": 33.33, "C": 33.33}, 10_000.0, "2024-01-02", "2024-01-04")
    assert r.initial_capital == pytest.approx(10_000.0)


def test_negative_weight_raises(cx):
    with pytest.raises(InvalidWeights):
        simulate(cx, {"A": 120.0, "B": -20.0}, 10_000.0, "2024-01-02")


def test_empty_allocation_raises(cx):
    with pytest.raises(InvalidWeights):
        simulate(cx, {}, 10_000.0, "2024-01-02")


def test_unknown_ticker_raises(cx):
    with pytest.raises(UnknownTicker):
        simulate(cx, {"A": 50.0, "ZZZZ": 50.0}, 10_000.0, "2024-01-02")


def test_start_before_available_history_raises(cx):
    # D n'existe qu'à partir du 04/01 : demander le 02/01 doit échouer, pas
    # démarrer silencieusement au 04 en ignorant les deux premiers jours.
    with pytest.raises(InsufficientHistory):
        simulate(cx, {"A": 50.0, "D": 50.0}, 10_000.0, "2024-01-02")


def test_end_after_available_history_raises(cx):
    with pytest.raises(InsufficientHistory):
        simulate(cx, ALLOC_REF, 10_000.0, "2024-01-02", "2024-06-01")


# ---------------------------------------------------------------------
#  Intégration — vraie base, valeurs extraites de boussole.db le 2026-07-30
# ---------------------------------------------------------------------
#   SELECT adjusted_close FROM prices_daily p JOIN instruments i USING(instrument_id)
#   WHERE i.ticker='IWDA' AND price_date='2024-03-01'   -> 88.800003
#   ... (idem CSPX, MEUD, aux deux dates) — voir la trace de session pour le
#   détail des six requêtes ayant produit ces nombres.
@pytest.mark.skipif(not DB_REELLE.exists(), reason="boussole.db absente — lancer ingest.py --init --full")
def test_reference_values_on_real_data():
    connexion = sqlite3.connect(DB_REELLE)
    connexion.row_factory = sqlite3.Row
    try:
        r = simulate(
            connexion,
            {"IWDA": 30.0, "CSPX": 30.0, "MEUD": 40.0},
            10_000.0,
            "2024-03-01",
            "2025-01-02",
        )
    finally:
        connexion.close()

    assert r.start_date == "2024-03-01"
    assert r.end_date == "2025-01-02"
    assert r.initial_capital == pytest.approx(10_000.0)
    assert r.final_value == pytest.approx(11_475.80, abs=0.05)
    assert r.gain_pct == pytest.approx(14.758, abs=0.005)

    holdings = {d.ticker: d for d in r.holdings}
    assert holdings["IWDA"].final_value == pytest.approx(3_557.43, abs=0.05)
    assert holdings["CSPX"].final_value == pytest.approx(3_692.25, abs=0.05)
    assert holdings["MEUD"].final_value == pytest.approx(4_226.12, abs=0.05)
