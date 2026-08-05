"""Tests du complément manuel d'historique (pipeline/manual_history.py).

Ces cours sont saisis à la main, souvent copiés depuis un site de courtier :
les tests portent donc sur les formats qu'on récupère réellement (dates
françaises, virgule décimale, espaces de milliers) et sur la règle de préséance,
qui est le point où une erreur détruirait des données de référence.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import manual_history
from manual_history import (ManualHistoryError, ManualQuote, insert, load_file,
                            resolve_adjusted, verify)

SCHEMA = Path(__file__).parent.parent / "schema.sql"


def write(tmp_path, name: str, content: str, encoding: str = "utf-8") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding=encoding)
    return p


# ---------------------------------------------------------------------
#  Lecture du fichier
# ---------------------------------------------------------------------
def test_minimal_iso_file(tmp_path):
    p = write(tmp_path, "MEUD.csv", "date,close\n2015-01-02,142.35\n2015-01-05,143.10\n")
    quotes = load_file(p)
    assert [(q.day, q.close) for q in quotes] == [
        (date(2015, 1, 2), 142.35),
        (date(2015, 1, 5), 143.10),
    ]
    # sans colonne dédiée, l'ajusté reste à calculer et aucun dividende n'est connu
    assert all(q.adjusted is None and q.dividend == 0 for q in quotes)


def test_french_date_and_decimal_comma(tmp_path):
    """Format typique d'un copier-coller depuis un site français."""
    p = write(tmp_path, "MEUD.csv", "date;close\n02/01/2015;142,35\n")
    assert [(q.day, q.close) for q in load_file(p)] == [(date(2015, 1, 2), 142.35)]


def test_thousands_separators_are_tolerated(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2015-01-02,\"1 234,56\"\n")
    assert load_file(p)[0].close == pytest.approx(1234.56)


def test_adjusted_close_column_is_used_when_present(tmp_path):
    p = write(tmp_path, "X.csv",
              "date,close,adjusted_close\n2015-01-02,142.35,138.90\n")
    assert load_file(p)[0].adjusted == pytest.approx(138.90)


def test_empty_adjusted_close_is_left_to_compute(tmp_path):
    p = write(tmp_path, "X.csv", "date,close,adjusted_close\n2015-01-02,142.35,\n")
    assert load_file(p)[0].adjusted is None


def test_dividend_column_is_read(tmp_path):
    p = write(tmp_path, "X.csv",
              "date,close,dividend\n2015-01-02,100,0\n2015-06-01,102,1.50\n")
    assert [q.dividend for q in load_file(p)] == [0.0, 1.50]


def test_rows_are_sorted_by_date(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2015-03-01,10\n2015-01-02,9\n")
    assert [q.day for q in load_file(p)] == [date(2015, 1, 2), date(2015, 3, 1)]


def test_bom_and_blank_lines(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2015-01-02,142.35\n\n", encoding="utf-8-sig")
    assert len(load_file(p)) == 1


# ---------------------------------------------------------------------
#  Refus — mieux vaut rien qu'un cours mal compris
# ---------------------------------------------------------------------
def test_unreadable_date_raises_with_line_number(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2 janvier 2015,142.35\n")
    with pytest.raises(ManualHistoryError, match="ligne 2"):
        load_file(p)


def test_non_numeric_price_raises(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2015-01-02,n/d\n")
    with pytest.raises(ManualHistoryError, match="n'est pas un nombre"):
        load_file(p)


def test_negative_or_zero_price_raises(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2015-01-02,0\n")
    with pytest.raises(ManualHistoryError, match="positif"):
        load_file(p)


def test_duplicate_date_raises(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2015-01-02,10\n2015-01-02,11\n")
    with pytest.raises(ManualHistoryError, match="déjà présente"):
        load_file(p)


def test_future_date_raises(tmp_path):
    demain = (date.today() + timedelta(days=1)).isoformat()
    p = write(tmp_path, "X.csv", f"date,close\n{demain},10\n")
    with pytest.raises(ManualHistoryError, match="futur"):
        load_file(p)


def test_missing_column_raises(tmp_path):
    p = write(tmp_path, "X.csv", "date,valeur\n2015-01-02,10\n")
    with pytest.raises(ManualHistoryError, match="close"):
        load_file(p)


def test_empty_file_raises(tmp_path):
    with pytest.raises(ManualHistoryError, match="vide"):
        load_file(write(tmp_path, "X.csv", ""))


def test_template_and_hidden_files_are_ignored(tmp_path):
    write(tmp_path, "_modele.csv", "date,close\n")
    write(tmp_path, "MEUD.csv", "date,close\n2015-01-02,10\n")
    assert set(manual_history.available_files(tmp_path)) == {"MEUD"}


# ---------------------------------------------------------------------
#  Préséance : le manuel ne doit JAMAIS écraser la source de référence
# ---------------------------------------------------------------------
@pytest.fixture()
def cx():
    connexion = sqlite3.connect(":memory:")
    connexion.row_factory = sqlite3.Row
    connexion.executescript(SCHEMA.read_text(encoding="utf-8"))
    connexion.execute(
        """INSERT INTO instruments (ticker, yahoo_symbol, name, asset_type, currency, exchange)
           VALUES ('MEUD', 'MEUD.PA', 'Amundi Stoxx 600', 'etf', 'EUR', 'Paris')""")
    connexion.commit()
    yield connexion
    connexion.close()


def add_yahoo(cx, day: str, close: float, adjusted: float | None = None):
    cx.execute(
        """INSERT INTO prices_daily (instrument_id, price_date, close, adjusted_close, source)
           VALUES (1, ?, ?, ?, 'yahoo')""",
        (day, close, adjusted if adjusted is not None else close))
    cx.commit()


def price_at(cx, day: str):
    r = cx.execute(
        "SELECT adjusted_close, source FROM prices_daily WHERE price_date = ?", (day,)
    ).fetchone()
    return (r["adjusted_close"], r["source"]) if r else None


def test_manual_fills_gaps_without_touching_yahoo(cx):
    add_yahoo(cx, "2024-03-01", 240.0)
    insert(cx, 1, [(date(2015, 1, 2), 140.0, 140.0),
                   (date(2024, 3, 1), 999.0, 999.0)], "MEUD")
    cx.commit()

    # la date que Yahoo couvre reste intacte
    assert price_at(cx, "2024-03-01") == (240.0, "yahoo")
    # la date manquante est comblée, et tracée comme manuelle
    assert price_at(cx, "2015-01-02") == (140.0, "manuel:MEUD")


def test_manual_rows_can_be_corrected_by_reimport(cx):
    insert(cx, 1, [(date(2015, 1, 2), 140.0, 140.0)], "MEUD")
    insert(cx, 1, [(date(2015, 1, 2), 141.5, 141.5)], "MEUD")
    cx.commit()
    assert price_at(cx, "2015-01-02") == (141.5, "manuel:MEUD")


def test_yahoo_reclaims_a_date_it_later_covers(cx):
    """Si Yahoo étend son historique, sa valeur doit reprendre la main."""
    insert(cx, 1, [(date(2015, 1, 2), 140.0, 140.0)], "MEUD")
    cx.commit()
    # l'upsert Yahoo d'ingest.py est inconditionnel : il écrase
    cx.execute(
        """INSERT INTO prices_daily (instrument_id, price_date, close, adjusted_close, source)
           VALUES (1, '2015-01-02', 138.0, 138.0, 'yahoo')
           ON CONFLICT (instrument_id, price_date) DO UPDATE SET
               adjusted_close = excluded.adjusted_close, source = excluded.source""")
    cx.commit()
    assert price_at(cx, "2015-01-02") == (138.0, "yahoo")


# ---------------------------------------------------------------------
#  Vérifications de cohérence du raccord
# ---------------------------------------------------------------------
def test_warns_when_instrument_distributes_dividends(cx):
    """Greffer du cours BRUT sur une série ajustée sous-estime le rendement."""
    add_yahoo(cx, "2024-03-01", 240.0, adjusted=200.0)   # brut != ajusté
    warnings = verify(cx, 1, "MEUD", [(date(2015, 1, 2), 140.0, 140.0)])
    assert any("DISTRIBUE" in w for w in warnings)


def test_no_warning_when_the_file_compensates(cx):
    """Fichier fournissant des dividendes : l'ajustement est reconstitué."""
    add_yahoo(cx, "2024-03-01", 240.0, adjusted=200.0)
    warnings = verify(cx, 1, "MEUD", [(date(2015, 1, 2), 140.0, 140.0)],
                      compensated=True)
    assert any("✓" in w and "reconstitué" in w for w in warnings)
    assert not any("DISTRIBUE" in w for w in warnings)


# ---------------------------------------------------------------------
#  Reconstitution du cours ajusté pour un ETF distribuant
# ---------------------------------------------------------------------
def test_yahoo_scale_factor_is_read_from_its_oldest_quote(cx):
    from manual_history import yahoo_scale_factor
    add_yahoo(cx, "2024-03-01", 200.0, adjusted=180.0)   # facteur 0,90
    add_yahoo(cx, "2024-03-04", 210.0, adjusted=195.0)
    scale, since = yahoo_scale_factor(cx, 1)
    assert scale == pytest.approx(0.90)
    assert since == "2024-03-01"


def test_raw_prices_are_scaled_onto_the_yahoo_series(cx):
    """Sans dividende saisi, le brut est quand même remis à l'échelle de Yahoo :
    les versements POSTÉRIEURS à la période saisie sont déjà dans son facteur."""
    add_yahoo(cx, "2024-03-01", 200.0, adjusted=180.0)   # facteur 0,90
    quotes = [ManualQuote(date(2015, 1, 2), 100.0, None, 0.0)]
    assert resolve_adjusted(cx, 1, quotes)[0][2] == pytest.approx(90.0)


def test_dividends_inside_the_manual_period_are_back_applied(cx):
    """Un détachement ne fait décrocher QUE les cours antérieurs."""
    add_yahoo(cx, "2024-03-01", 200.0, adjusted=200.0)   # facteur 1
    quotes = [
        ManualQuote(date(2015, 1, 2), 100.0, None, 0.0),
        ManualQuote(date(2015, 6, 1), 102.0, None, 2.0),   # détachement de 2
        ManualQuote(date(2015, 9, 1), 105.0, None, 0.0),
    ]
    resolved = resolve_adjusted(cx, 1, quotes)
    # après le détachement : inchangé
    assert resolved[2][2] == pytest.approx(105.0)
    assert resolved[1][2] == pytest.approx(102.0)
    # avant : réduit de (1 - 2/100), le 100 étant la clôture de la veille
    assert resolved[0][2] == pytest.approx(100.0 * (1 - 2 / 100.0))


def test_explicit_adjusted_close_always_wins(cx):
    add_yahoo(cx, "2024-03-01", 200.0, adjusted=180.0)
    quotes = [ManualQuote(date(2015, 1, 2), 100.0, 77.0, 0.0)]
    assert resolve_adjusted(cx, 1, quotes)[0][2] == pytest.approx(77.0)


def test_without_yahoo_data_the_scale_stays_neutral(cx):
    quotes = [ManualQuote(date(2015, 1, 2), 100.0, None, 0.0)]
    assert resolve_adjusted(cx, 1, quotes)[0][2] == pytest.approx(100.0)


def test_no_dividend_warning_for_accumulating(cx):
    add_yahoo(cx, "2024-03-01", 240.0)                   # brut == ajusté
    warnings = verify(cx, 1, "MEUD", [(date(2015, 1, 2), 140.0, 140.0)])
    assert not any("distribue" in w for w in warnings)


def test_overlap_agreement_is_reported_as_a_check_passed(cx):
    add_yahoo(cx, "2024-03-01", 240.0)
    warnings = verify(cx, 1, "MEUD", [(date(2024, 3, 1), 240.2, 240.2)])
    assert any("✓" in w and "concorde" in w for w in warnings)


def test_overlap_disagreement_is_flagged(cx):
    add_yahoo(cx, "2024-03-01", 240.0)
    warnings = verify(cx, 1, "MEUD", [(date(2024, 3, 1), 300.0, 300.0)])
    assert any("écart atteint" in w for w in warnings)


def test_implausible_jump_at_the_splice_is_flagged(cx):
    add_yahoo(cx, "2024-03-04", 240.0)
    warnings = verify(cx, 1, "MEUD", [(date(2024, 3, 1), 150.0, 150.0)])
    assert any("marche" in w for w in warnings)


def test_continuous_splice_is_silent(cx):
    add_yahoo(cx, "2024-03-04", 240.0)
    warnings = verify(cx, 1, "MEUD", [(date(2024, 3, 1), 239.0, 239.0)])
    assert warnings == []
