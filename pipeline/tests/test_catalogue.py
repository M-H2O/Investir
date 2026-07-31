"""Tests du chargeur de catalogue (pipeline/catalogue.py).

Le catalogue est édité à la main, souvent dans un tableur : ces tests couvrent
donc surtout ce qu'un tableur produit réellement (point-virgule, BOM, espaces,
lignes vides) et les erreurs de saisie qui doivent ARRÊTER l'ingestion plutôt
que de faire disparaître un instrument en silence.
"""

from __future__ import annotations

import pytest

from catalogue import CatalogueError, Instrument, all_instruments, load


def write(tmp_path, content: str, encoding: str = "utf-8"):
    p = tmp_path / "tickers.csv"
    p.write_text(content, encoding=encoding)
    return p


# ---------------------------------------------------------------------
#  Cas nominal
# ---------------------------------------------------------------------
def test_minimal_file_only_needs_the_symbol_column(tmp_path):
    p = write(tmp_path, "yahoo_symbol\nSXR8.DE\nAAPL\n")
    instruments = load(p)

    assert [i.yahoo for i in instruments] == ["SXR8.DE", "AAPL"]
    # ticker d'affichage déduit du symbole, suffixe de place retiré
    assert [i.ticker for i in instruments] == ["SXR8", "AAPL"]
    # tout le reste sera résolu depuis Yahoo à l'ingestion
    assert all(i.needs_resolution for i in instruments)
    assert all(i.active for i in instruments)


def test_full_file_is_read_as_written(tmp_path):
    p = write(tmp_path,
              "yahoo_symbol,ticker,name,isin,asset_type,currency,exchange,active\n"
              "SXR8.DE,CSPX,iShares Core S&P 500,IE00B5BMR087,etf,EUR,XETRA,oui\n")
    (i,) = load(p)

    assert i == Instrument(yahoo="SXR8.DE", ticker="CSPX",
                           name="iShares Core S&P 500", isin="IE00B5BMR087",
                           asset_type="etf", currency="EUR", exchange="XETRA",
                           active=True)
    assert not i.needs_resolution


def test_display_ticker_overrides_the_derived_one(tmp_path):
    p = write(tmp_path, "yahoo_symbol,ticker\nSXR8.DE,CSPX\n")
    (i,) = load(p)
    assert i.ticker == "CSPX"


# ---------------------------------------------------------------------
#  Ce qu'un tableur produit réellement
# ---------------------------------------------------------------------
def test_semicolon_separator_from_french_excel(tmp_path):
    """Excel en configuration française enregistre les CSV avec des `;`."""
    p = write(tmp_path, "yahoo_symbol;ticker;currency\nSXR8.DE;CSPX;EUR\n")
    (i,) = load(p)
    assert (i.yahoo, i.ticker, i.currency) == ("SXR8.DE", "CSPX", "EUR")


def test_utf8_bom_is_stripped(tmp_path):
    """Excel préfixe ses CSV d'un BOM ; sans le retirer, la première colonne
    s'appellerait '\\ufeffyahoo_symbol' et ne serait pas reconnue."""
    p = write(tmp_path, "yahoo_symbol,ticker\nSXR8.DE,CSPX\n", encoding="utf-8-sig")
    (i,) = load(p)
    assert i.yahoo == "SXR8.DE"


def test_surrounding_spaces_and_header_case_are_tolerated(tmp_path):
    p = write(tmp_path, " Yahoo_Symbol , TICKER \n  SXR8.DE  ,  CSPX  \n")
    (i,) = load(p)
    assert (i.yahoo, i.ticker) == ("SXR8.DE", "CSPX")


def test_blank_lines_are_skipped(tmp_path):
    p = write(tmp_path, "yahoo_symbol\nSXR8.DE\n\n,\nAAPL\n")
    assert [i.yahoo for i in load(p)] == ["SXR8.DE", "AAPL"]


# ---------------------------------------------------------------------
#  Activation / désactivation
# ---------------------------------------------------------------------
@pytest.mark.parametrize("value", ["non", "NON", "no", "0", "false", "faux", "inactif"])
def test_line_can_be_disabled_without_deleting_it(tmp_path, value):
    p = write(tmp_path, f"yahoo_symbol,active\nSXR8.DE,{value}\nAAPL,oui\n")
    assert [i.active for i in load(p)] == [False, True]
    # all_instruments ne renvoie que l'actif : c'est lui que le pipeline traite
    assert [i.yahoo for i in all_instruments(p)] == ["AAPL"]


def test_empty_active_cell_means_active(tmp_path):
    """Une case vide ne doit jamais faire disparaître un instrument."""
    p = write(tmp_path, "yahoo_symbol,active\nSXR8.DE,\n")
    assert load(p)[0].active is True


# ---------------------------------------------------------------------
#  Erreurs de saisie — doivent arrêter net, avec le numéro de ligne
# ---------------------------------------------------------------------
def test_missing_required_column_raises(tmp_path):
    p = write(tmp_path, "ticker,name\nCSPX,iShares\n")
    with pytest.raises(CatalogueError, match="yahoo_symbol"):
        load(p)


def test_unknown_column_raises(tmp_path):
    """Une colonne mal orthographiée serait sinon ignorée en silence."""
    p = write(tmp_path, "yahoo_symbol,devise\nSXR8.DE,EUR\n")
    with pytest.raises(CatalogueError, match="devise"):
        load(p)


def test_duplicate_symbol_raises_with_line_numbers(tmp_path):
    p = write(tmp_path, "yahoo_symbol\nSXR8.DE\nAAPL\nSXR8.DE\n")
    with pytest.raises(CatalogueError, match="ligne 4"):
        load(p)


def test_duplicate_display_ticker_raises(tmp_path):
    """Deux lignes différentes ne peuvent pas porter le même ticker : il sert
    de clé côté site."""
    p = write(tmp_path, "yahoo_symbol,ticker\nSXR8.DE,CSPX\nCSSPX.MI,CSPX\n")
    with pytest.raises(CatalogueError, match="déjà"):
        load(p)


def test_row_with_data_but_no_symbol_raises(tmp_path):
    p = write(tmp_path, "yahoo_symbol,name\nSXR8.DE,iShares\n,Orphelin\n")
    with pytest.raises(CatalogueError, match="ligne 3"):
        load(p)


def test_invalid_asset_type_raises(tmp_path):
    p = write(tmp_path, "yahoo_symbol,asset_type\nSXR8.DE,obligation\n")
    with pytest.raises(CatalogueError, match="asset_type"):
        load(p)


def test_missing_file_raises(tmp_path):
    with pytest.raises(CatalogueError, match="introuvable"):
        load(tmp_path / "absent.csv")


def test_empty_file_raises(tmp_path):
    with pytest.raises(CatalogueError, match="vide"):
        load(write(tmp_path, ""))


def test_header_only_raises(tmp_path):
    p = write(tmp_path, "yahoo_symbol\n")
    with pytest.raises(CatalogueError, match="aucun instrument"):
        load(p)


# ---------------------------------------------------------------------
#  Le catalogue réel du projet doit rester valide
# ---------------------------------------------------------------------
def test_project_catalogue_loads():
    instruments = all_instruments()
    assert len(instruments) >= 1
    assert len({i.yahoo for i in instruments}) == len(instruments)
    assert len({i.ticker for i in instruments}) == len(instruments)
