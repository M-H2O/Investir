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
from manual_history import ManualHistoryError, insert, load_file, verify

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
    assert load_file(p) == [
        (date(2015, 1, 2), 142.35, 142.35),
        (date(2015, 1, 5), 143.10, 143.10),
    ]


def test_french_date_and_decimal_comma(tmp_path):
    """Format typique d'un copier-coller depuis un site français."""
    p = write(tmp_path, "MEUD.csv", "date;close\n02/01/2015;142,35\n")
    assert load_file(p) == [(date(2015, 1, 2), 142.35, 142.35)]


def test_thousands_separators_are_tolerated(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2015-01-02,\"1 234,56\"\n")
    assert load_file(p)[0][1] == pytest.approx(1234.56)


def test_adjusted_close_column_is_used_when_present(tmp_path):
    p = write(tmp_path, "X.csv",
              "date,close,adjusted_close\n2015-01-02,142.35,138.90\n")
    assert load_file(p) == [(date(2015, 1, 2), 142.35, 138.90)]


def test_adjusted_close_defaults_to_close(tmp_path):
    p = write(tmp_path, "X.csv", "date,close,adjusted_close\n2015-01-02,142.35,\n")
    assert load_file(p) == [(date(2015, 1, 2), 142.35, 142.35)]


def test_rows_are_sorted_by_date(tmp_path):
    p = write(tmp_path, "X.csv", "date,close\n2015-03-01,10\n2015-01-02,9\n")
    assert [r[0] for r in load_file(p)] == [date(2015, 1, 2), date(2015, 3, 1)]


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
    assert any("distribue" in w for w in warnings)


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
