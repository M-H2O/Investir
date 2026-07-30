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
    HistoriqueInsuffisant,
    PoidsInvalides,
    TickerInconnu,
    simuler,
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
def test_valeur_finale_et_gain(cx):
    r = simuler(cx, ALLOC_REF, 10_000.0, "2024-01-02", "2024-01-04")

    assert r.date_debut == "2024-01-02"
    assert r.date_fin == "2024-01-04"
    assert r.capital_initial == pytest.approx(10_000.0)
    assert r.valeur_finale == pytest.approx(11_050.0)
    assert r.gain_absolu == pytest.approx(1_050.0)
    assert r.gain_pct == pytest.approx(10.5)


def test_detail_par_instrument(cx):
    r = simuler(cx, ALLOC_REF, 10_000.0, "2024-01-02", "2024-01-04")
    detail = {d.ticker: d for d in r.detail}

    assert detail["A"].prix_debut == pytest.approx(100.0)
    assert detail["A"].prix_fin == pytest.approx(105.0)
    assert detail["A"].capital_investi == pytest.approx(5_000.0)
    assert detail["A"].valeur_finale == pytest.approx(5_250.0)
    assert detail["A"].gain_pct == pytest.approx(5.0)

    assert detail["B"].valeur_finale == pytest.approx(3_600.0)
    assert detail["B"].gain_pct == pytest.approx(20.0)

    assert detail["C"].valeur_finale == pytest.approx(2_200.0)
    assert detail["C"].gain_pct == pytest.approx(10.0)


def test_serie_temporelle_et_report_jour_ferie(cx):
    """C n'a pas de cotation le 03/01 : sa valeur ce jour-là doit être reportée
    depuis le 02/01 (200.00), pas absente ni à zéro — c'est le comportement que
    consommerait un curseur de date dans l'interface."""
    r = simuler(cx, ALLOC_REF, 10_000.0, "2024-01-02", "2024-01-04")
    serie = dict(r.serie)

    assert list(serie.keys()) == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert serie["2024-01-02"] == pytest.approx(10_000.0)
    assert serie["2024-01-03"] == pytest.approx(10_500.0)
    assert serie["2024-01-04"] == pytest.approx(11_050.0)


def test_un_seul_instrument_suit_exactement_son_cours(cx):
    r = simuler(cx, {"A": 100.0}, 1_000.0, "2024-01-02", "2024-01-04")
    # 100% sur une seule ligne : la valeur suit exactement le ratio de prix.
    assert r.valeur_finale == pytest.approx(1_000.0 * 105.0 / 100.0)
    assert r.gain_pct == pytest.approx(5.0)


def test_date_fin_par_defaut_est_le_dernier_jour_commun(cx):
    r = simuler(cx, ALLOC_REF, 10_000.0, "2024-01-02")
    assert r.date_fin == "2024-01-04"


# ---------------------------------------------------------------------
#  Erreurs — doivent être explicites, jamais un résultat silencieusement faux
# ---------------------------------------------------------------------
def test_poids_ne_totalisant_pas_100_leve_une_erreur(cx):
    with pytest.raises(PoidsInvalides):
        simuler(cx, {"A": 30.0, "B": 30.0, "C": 30.0}, 10_000.0, "2024-01-02")


def test_tolerance_arrondi_flottant_acceptee(cx):
    # 33.33 + 33.33 + 33.34 = 100.00 pile, mais un budget UI en float peut
    # produire 99.999999... à l'epsilon près : la tolérance doit l'absorber.
    r = simuler(cx, {"A": 33.34, "B": 33.33, "C": 33.33}, 10_000.0, "2024-01-02", "2024-01-04")
    assert r.capital_initial == pytest.approx(10_000.0)


def test_poids_negatif_leve_une_erreur(cx):
    with pytest.raises(PoidsInvalides):
        simuler(cx, {"A": 120.0, "B": -20.0}, 10_000.0, "2024-01-02")


def test_allocation_vide_leve_une_erreur(cx):
    with pytest.raises(PoidsInvalides):
        simuler(cx, {}, 10_000.0, "2024-01-02")


def test_ticker_inconnu_leve_une_erreur(cx):
    with pytest.raises(TickerInconnu):
        simuler(cx, {"A": 50.0, "ZZZZ": 50.0}, 10_000.0, "2024-01-02")


def test_date_debut_anterieure_a_historique_leve_une_erreur(cx):
    # D n'existe qu'à partir du 04/01 : demander le 02/01 doit échouer, pas
    # démarrer silencieusement au 04 en ignorant les deux premiers jours.
    with pytest.raises(HistoriqueInsuffisant):
        simuler(cx, {"A": 50.0, "D": 50.0}, 10_000.0, "2024-01-02")


def test_date_fin_posterieure_a_historique_leve_une_erreur(cx):
    with pytest.raises(HistoriqueInsuffisant):
        simuler(cx, ALLOC_REF, 10_000.0, "2024-01-02", "2024-06-01")


# ---------------------------------------------------------------------
#  Intégration — vraie base, valeurs extraites de boussole.db le 2026-07-30
# ---------------------------------------------------------------------
#   SELECT adjusted_close FROM prices_daily p JOIN instruments i USING(instrument_id)
#   WHERE i.ticker='IWDA' AND price_date='2024-03-01'   -> 88.800003
#   ... (idem CSPX, MEUD, aux deux dates) — voir la trace de session pour le
#   détail des six requêtes ayant produit ces nombres.
@pytest.mark.skipif(not DB_REELLE.exists(), reason="boussole.db absente — lancer ingest.py --init --full")
def test_reference_donnees_reelles():
    connexion = sqlite3.connect(DB_REELLE)
    connexion.row_factory = sqlite3.Row
    try:
        r = simuler(
            connexion,
            {"IWDA": 30.0, "CSPX": 30.0, "MEUD": 40.0},
            10_000.0,
            "2024-03-01",
            "2025-01-02",
        )
    finally:
        connexion.close()

    assert r.date_debut == "2024-03-01"
    assert r.date_fin == "2025-01-02"
    assert r.capital_initial == pytest.approx(10_000.0)
    assert r.valeur_finale == pytest.approx(11_475.80, abs=0.05)
    assert r.gain_pct == pytest.approx(14.758, abs=0.005)

    detail = {d.ticker: d for d in r.detail}
    assert detail["IWDA"].valeur_finale == pytest.approx(3_557.43, abs=0.05)
    assert detail["CSPX"].valeur_finale == pytest.approx(3_692.25, abs=0.05)
    assert detail["MEUD"].valeur_finale == pytest.approx(4_226.12, abs=0.05)
