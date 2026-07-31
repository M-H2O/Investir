"""Catalogue des instruments, lu depuis `data/tickers.csv`.

La liste se pilote à la main dans ce CSV — éditable dans Excel, Google Sheets
ou directement sur GitHub — sans jamais toucher au code. Le fichier reste du
texte : les diffs GitHub restent lisibles et un conflit reste résoluble, ce
qu'un classeur .xlsx ne permet pas.

Seule la colonne `yahoo_symbol` est OBLIGATOIRE. Tout le reste est facultatif
et sera complété automatiquement depuis Yahoo à l'ingestion (devise, place,
type, nom). Un fichier d'une seule colonne est donc parfaitement valide.

Colonnes reconnues
------------------
    yahoo_symbol   obligatoire — le symbole Yahoo, ex. SXR8.DE, 0GGH.L, AAPL
                   ATTENTION : ce n'est presque jamais le ticker d'affichage
                   (CSPX -> SXR8.DE). Le résoudre par ISIN :
                   https://query1.finance.yahoo.com/v1/finance/search?q=<ISIN>
    ticker         nom court affiché sur le site ; par défaut le symbole
                   sans son suffixe de place (SXR8.DE -> SXR8)
    name           libellé long ; récupéré depuis Yahoo si laissé vide
    isin           informatif, Yahoo ne le fournit pas de façon fiable
    asset_type     'etf' ou 'stock' ; déduit de Yahoo si vide
    currency       devise de cotation ; déduite de Yahoo si vide
    exchange       place de cotation ; déduite de Yahoo si vide
    active         'non' pour retirer une ligne du site sans la supprimer
                   du fichier ; toute autre valeur (ou vide) = active

Le séparateur (`,` ou `;`) est détecté automatiquement : Excel en configuration
française enregistre les CSV avec des points-virgules, et le fichier resterait
illisible sans cette détection.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

CATALOGUE_PATH = Path(__file__).parent.parent / "data" / "tickers.csv"

REQUIRED_COLUMN = "yahoo_symbol"
KNOWN_COLUMNS = {
    "yahoo_symbol", "ticker", "name", "isin",
    "asset_type", "currency", "exchange", "active",
}
# Valeurs comprises comme « ligne désactivée ». Tout le reste vaut actif :
# une case vide ne doit jamais faire disparaître un instrument en silence.
FALSY = {"non", "no", "n", "0", "false", "faux", "inactif", "inactive"}


class CatalogueError(Exception):
    """Le fichier est inutilisable — on refuse de deviner ce qu'il voulait dire."""


@dataclass(frozen=True)
class Instrument:
    yahoo: str                       # symbole de récupération Yahoo
    ticker: str                      # ticker d'affichage
    name: str | None = None          # complété depuis Yahoo si absent
    isin: str | None = None
    asset_type: str | None = None
    currency: str | None = None
    exchange: str | None = None
    active: bool = True

    @property
    def needs_resolution(self) -> bool:
        """Reste-t-il des champs à aller chercher chez Yahoo ?"""
        return not all([self.name, self.asset_type, self.currency, self.exchange])


def _sniff_delimiter(sample: str) -> str:
    """Point-virgule ou virgule ? On tranche sur la ligne d'en-tête.

    csv.Sniffer se trompe sur un fichier d'une seule colonne sans séparateur ;
    compter sur l'en-tête est plus sûr pour le format simple qu'on attend ici.
    """
    header = sample.splitlines()[0] if sample.splitlines() else ""
    return ";" if header.count(";") > header.count(",") else ","


def _clean(value) -> str | None:
    """Normalise une cellule. Tolère autre chose qu'une chaîne : quand une ligne
    compte plus de champs que d'en-têtes (virgule surnuméraire laissée par un
    tableur), csv.DictReader range le surplus dans une LISTE sous la clé None.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = " ".join(v for v in value if v)
    value = str(value).strip().strip('"').strip()
    return value or None


def load(path: Path | None = None) -> list[Instrument]:
    """Lit le catalogue. Lève `CatalogueError` avec le numéro de ligne fautif
    plutôt que d'ingérer silencieusement une liste incomplète."""
    path = path or CATALOGUE_PATH
    if not path.exists():
        raise CatalogueError(
            f"Catalogue introuvable : {path}\n"
            f"Créez-le avec au minimum une colonne '{REQUIRED_COLUMN}'."
        )

    # utf-8-sig retire le BOM qu'Excel ajoute systématiquement en tête.
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise CatalogueError(f"Catalogue vide : {path}")

    reader = csv.DictReader(io.StringIO(text), delimiter=_sniff_delimiter(text))
    if not reader.fieldnames:
        raise CatalogueError(f"Catalogue sans ligne d'en-tête : {path}")

    # En-têtes tolérants à la casse et aux espaces ajoutés par un tableur.
    headers = {(h or "").strip().lower(): h for h in reader.fieldnames}
    if REQUIRED_COLUMN not in headers:
        raise CatalogueError(
            f"Colonne '{REQUIRED_COLUMN}' absente de {path.name}.\n"
            f"Colonnes trouvées : {', '.join(reader.fieldnames)}"
        )
    unknown = set(headers) - KNOWN_COLUMNS - {""}
    if unknown:
        raise CatalogueError(
            f"Colonne(s) non reconnue(s) dans {path.name} : {', '.join(sorted(unknown))}.\n"
            f"Attendu : {', '.join(sorted(KNOWN_COLUMNS))}"
        )

    get = lambda row, col: _clean(row.get(headers[col])) if col in headers else None

    instruments: list[Instrument] = []
    seen_symbols: dict[str, int] = {}
    seen_tickers: dict[str, int] = {}

    for line_no, row in enumerate(reader, start=2):   # 1 = en-tête
        symbol = get(row, "yahoo_symbol")
        if symbol is None:
            if any(_clean(v) for v in row.values()):
                raise CatalogueError(
                    f"{path.name} ligne {line_no} : '{REQUIRED_COLUMN}' est vide "
                    f"alors que la ligne contient des données."
                )
            continue                                  # ligne totalement vide : on passe

        if symbol in seen_symbols:
            raise CatalogueError(
                f"{path.name} ligne {line_no} : symbole '{symbol}' déjà présent "
                f"ligne {seen_symbols[symbol]}."
            )
        seen_symbols[symbol] = line_no

        # Par défaut, le ticker d'affichage est le symbole sans suffixe de place.
        ticker = get(row, "ticker") or symbol.split(".")[0]
        if ticker in seen_tickers:
            raise CatalogueError(
                f"{path.name} ligne {line_no} : ticker d'affichage '{ticker}' déjà "
                f"utilisé ligne {seen_tickers[ticker]}. Renseignez une colonne "
                f"'ticker' distincte pour les départager."
            )
        seen_tickers[ticker] = line_no

        active_raw = get(row, "active")
        asset_type = get(row, "asset_type")
        if asset_type and asset_type.lower() not in {"etf", "stock"}:
            raise CatalogueError(
                f"{path.name} ligne {line_no} : asset_type '{asset_type}' invalide "
                f"(attendu 'etf' ou 'stock')."
            )

        instruments.append(Instrument(
            yahoo=symbol,
            ticker=ticker,
            name=get(row, "name"),
            isin=get(row, "isin"),
            asset_type=asset_type.lower() if asset_type else None,
            currency=get(row, "currency"),
            exchange=get(row, "exchange"),
            active=(active_raw or "").lower() not in FALSY,
        ))

    if not instruments:
        raise CatalogueError(f"{path.name} ne contient aucun instrument.")
    return instruments


def all_instruments(path: Path | None = None) -> list[Instrument]:
    """Les seuls instruments actifs — c'est ce que le pipeline doit traiter."""
    return [i for i in load(path) if i.active]


# Paires de change, ingérées dans `fx_rates`. Inutiles tant que tout cote en
# euros ; à activer dès l'ajout d'instruments en devise étrangère.
# Convention Yahoo : 'EURUSD=X' = combien d'USD pour 1 EUR.
FX_PAIRS: dict[str, str] = {
    "EUR/USD": "EURUSD=X",
    "EUR/CHF": "EURCHF=X",
    "EUR/GBP": "EURGBP=X",
}
