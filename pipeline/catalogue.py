"""Catalogue des instruments à ingérer.

Les symboles Yahoo ci-dessous ont été RÉSOLUS par recherche ISIN puis vérifiés
un par un (devise, place, profondeur d'historique) — ils ne sont pas déduits du
ticker d'affichage, qui ne correspond presque jamais.

Règle de sélection : à ISIN égal, on retient la ligne cotée EN EUROS et la plus
profonde en historique. Le simulateur raisonne en euros ; passer par le change
ajouterait une source d'erreur là où une cotation native existe.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    ticker: str        # ticker d'affichage, celui du comparateur
    yahoo: str         # symbole de récupération Yahoo
    isin: str
    name: str
    asset_type: str    # 'etf' | 'stock'
    currency: str
    exchange: str
    # Première cotation disponible chez Yahoo, constatée à la résolution.
    # Sert d'alerte : ce n'est PAS la date de création du fonds.
    yahoo_since: str


ETFS: list[Instrument] = [
    # --- Monde ---------------------------------------------------------
    Instrument("IWDA",  "IWDA.AS", "IE00B4L5Y983", "iShares Core MSCI World",             "etf", "EUR", "Amsterdam", "2009-09-30"),
    Instrument("VWCE",  "VWCE.DE", "IE00BK5BQT80", "Vanguard FTSE All-World (acc.)",      "etf", "EUR", "XETRA",     "2019-07-28"),
    Instrument("VWRL",  "VWRL.AS", "IE00B3RBWM25", "Vanguard FTSE All-World (dist.)",     "etf", "EUR", "Amsterdam", "2012-05-31"),
    Instrument("WEBN",  "WEBN.DE", "IE0003XJA0J9", "Amundi Prime All Country World",      "etf", "EUR", "XETRA",     "2024-07-14"),
    Instrument("CW8",   "CW8.PA",  "LU1681043599", "Amundi MSCI World (PEA)",             "etf", "EUR", "Paris",     "2009-06-30"),
    Instrument("WPEA",  "WPEA.PA", "IE0002XZSHO1", "iShares MSCI World Swap PEA",         "etf", "EUR", "Paris",     "2024-03-31"),
    Instrument("EWLD",  "EWLD.PA", "FR0011869353", "Amundi PEA Monde",                    "etf", "EUR", "Paris",     "2024-03-10"),
    Instrument("TDIV",  "TDIV.AS", "NL0011683594", "VanEck Morningstar Dev. Dividend",    "etf", "EUR", "Amsterdam", "2016-05-31"),

    # --- USA -----------------------------------------------------------
    Instrument("CSPX",  "SXR8.DE", "IE00B5BMR087", "iShares Core S&P 500",                "etf", "EUR", "XETRA",     "2010-05-31"),
    Instrument("SPYL",  "SPYL.DE", "IE000XZSV718", "SPDR S&P 500",                        "etf", "EUR", "XETRA",     "2023-10-29"),
    Instrument("PE500", "PSP5.PA", "FR0011871128", "Amundi PEA S&P 500",                  "etf", "EUR", "Paris",     "2014-05-31"),
    Instrument("ESE",   "ESE.PA",  "FR0011550185", "BNP Paribas Easy S&P 500 (PEA)",      "etf", "EUR", "Paris",     "2013-09-30"),
    Instrument("PANX",  "PUST.PA", "FR0011871110", "Amundi PEA Nasdaq-100",               "etf", "EUR", "Paris",     "2014-05-31"),

    # --- Europe --------------------------------------------------------
    Instrument("MEUD",  "MEUD.PA", "LU0908500753", "Amundi Stoxx Europe 600 (PEA)",       "etf", "EUR", "Paris",     "2024-02-18"),
    Instrument("IMAE",  "IMAE.AS", "IE00B4K48X80", "iShares Core MSCI Europe",            "etf", "EUR", "Amsterdam", "2009-10-31"),
    Instrument("CACC",  "CACC.PA", "FR0013380607", "Amundi CAC 40 (PEA)",                 "etf", "EUR", "Paris",     "2018-12-09"),

    # --- Émergents -----------------------------------------------------
    Instrument("EIMI",  "IS3N.DE", "IE00BKM4GZ66", "iShares Core MSCI EM IMI",            "etf", "EUR", "XETRA",     "2014-06-30"),
    Instrument("PAEEM", "PAEEM.PA", "FR0013412020", "Amundi PEA Emergent ESG Transition", "etf", "EUR", "Paris",     "2019-04-21"),

    # --- Obligations ---------------------------------------------------
    Instrument("AGGH",  "0GGH.L",  "IE00BDBRDM35", "iShares Core Global Aggregate (EUR-H)", "etf", "EUR", "LSE",     "2017-11-20"),
    Instrument("EM710", "MTD.PA",  "LU1287023185", "Amundi Euro Government Bond 7-10Y",   "etf", "EUR", "Paris",     "2009-01-31"),
]

# Actions individuelles : à compléter au fil de l'eau. Le script les traite
# exactement comme les ETF — seul `asset_type` change.
# Attention : une action américaine cote en USD, ce qui rendra `fx_rates`
# nécessaire pour simuler en euros (voir FX_PAIRS).
STOCKS: list[Instrument] = [
    # Instrument("AAPL", "AAPL", "US0378331005", "Apple Inc.", "stock", "USD", "NASDAQ", "1980-12-12"),
]

# Paires de change, ingérées dans `fx_rates`. Inutiles tant que tout cote en
# euros ; à activer dès l'ajout d'instruments en devise étrangère.
# Convention Yahoo : 'EURUSD=X' = combien d'USD pour 1 EUR.
FX_PAIRS: dict[str, str] = {
    "EUR/USD": "EURUSD=X",
    "EUR/CHF": "EURCHF=X",
    "EUR/GBP": "EURGBP=X",
}


def all_instruments() -> list[Instrument]:
    return ETFS + STOCKS
