"""Moteur de calcul de portefeuille — simulation « achat unique, sans
rééquilibrage » : on répartit un capital selon des poids fixes à une date de
départ, puis on laisse chaque ligne suivre son propre cours jusqu'à la date de
fin. Aucun arbitrage automatique entre les lignes n'est simulé — si vous voulez
un jour un rééquilibrage périodique, c'est une fonction séparée, pas une option
cachée de celle-ci.

Toutes les valeurs sont calculées sur `adjusted_close` (dividendes et splits
réintégrés) : c'est la seule colonne qui représente fidèlement la performance
totale d'une ligne détenue dans la durée.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


class ErreurSimulation(Exception):
    """Base commune : toute erreur remontée par ce module en hérite."""


class PoidsInvalides(ErreurSimulation):
    pass


class TickerInconnu(ErreurSimulation):
    pass


class HistoriqueInsuffisant(ErreurSimulation):
    pass


# Tolérance sur la somme des poids : absorbe l'arrondi flottant d'une UI à
# sliders (33.33 + 33.33 + 33.34) sans laisser passer une vraie erreur de
# saisie (30 + 30 + 30 = 90 doit échouer).
TOLERANCE_POIDS_PCT = 0.1


@dataclass
class DetailInstrument:
    ticker: str
    poids_pct: float
    prix_debut: float
    prix_fin: float
    capital_investi: float
    valeur_finale: float

    @property
    def gain_pct(self) -> float:
        return (self.valeur_finale / self.capital_investi - 1) * 100


@dataclass
class ResultatSimulation:
    date_debut: str          # date effectivement utilisée (peut différer de la
    date_fin: str             # date demandée si ce n'était pas un jour coté)
    capital_initial: float
    valeur_finale: float
    detail: list[DetailInstrument]
    # (date, valeur_totale) pour chaque jour coté de la période — c'est la
    # série que consomme un curseur temporel dans l'interface.
    serie: list[tuple[str, float]] = field(repr=False)

    @property
    def gain_absolu(self) -> float:
        return self.valeur_finale - self.capital_initial

    @property
    def gain_pct(self) -> float:
        return (self.valeur_finale / self.capital_initial - 1) * 100


def _valider_poids(allocation: dict[str, float]) -> None:
    if not allocation:
        raise PoidsInvalides("l'allocation est vide")
    total = sum(allocation.values())
    if abs(total - 100.0) > TOLERANCE_POIDS_PCT:
        raise PoidsInvalides(
            f"les poids totalisent {total:.2f} %, pas 100 % "
            f"(tolérance ±{TOLERANCE_POIDS_PCT} point)"
        )
    negatifs = [t for t, p in allocation.items() if p < 0]
    if negatifs:
        raise PoidsInvalides(f"poids négatif pour : {', '.join(negatifs)}")


def _instrument_id(cx: sqlite3.Connection, ticker: str) -> int:
    row = cx.execute(
        "SELECT instrument_id FROM instruments WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row is None:
        raise TickerInconnu(f"« {ticker} » n'existe pas dans le catalogue")
    return row["instrument_id"]


def _serie_prix(cx: sqlite3.Connection, instrument_id: int) -> dict[str, float]:
    """Toutes les cotations d'un instrument, sous forme {date: adjusted_close}."""
    rows = cx.execute(
        """SELECT price_date, adjusted_close FROM prices_daily
           WHERE instrument_id = ? AND adjusted_close IS NOT NULL
           ORDER BY price_date""",
        (instrument_id,),
    ).fetchall()
    return {r["price_date"]: r["adjusted_close"] for r in rows}


def simuler(
    cx: sqlite3.Connection,
    allocation: dict[str, float],
    capital_initial: float,
    date_debut: str,
    date_fin: str | None = None,
) -> ResultatSimulation:
    """Simule un investissement unique de `capital_initial` réparti selon
    `allocation` ({ticker: poids en %}), à partir de `date_debut`.

    `date_fin` par défaut = la dernière date où TOUTES les lignes de
    l'allocation ont une cotation — jamais une date où l'une d'elles serait
    silencieusement absente.

    Lève `HistoriqueInsuffisant` plutôt que de démarrer plus tard que demandé
    sans le dire : si vous simulez "il y a 10 ans" avec une ligne qui n'existe
    que depuis 2 ans, l'appelant doit le savoir, pas le déduire d'un résultat
    qui commence ailleurs que prévu.
    """
    _valider_poids(allocation)

    series: dict[str, dict[str, float]] = {}
    for ticker in allocation:
        iid = _instrument_id(cx, ticker)
        serie = _serie_prix(cx, iid)
        if not serie:
            raise HistoriqueInsuffisant(f"« {ticker} » n'a aucune cotation en base")
        series[ticker] = serie

    # Première date cotée >= date_debut, pour chaque ligne.
    debuts_par_ticker: dict[str, str] = {}
    for ticker, serie in series.items():
        dispo = sorted(d for d in serie if d >= date_debut)
        if not dispo:
            raise HistoriqueInsuffisant(
                f"« {ticker} » n'a aucune cotation à partir du {date_debut}"
            )
        debuts_par_ticker[ticker] = dispo[0]

    premiere_cotation = {t: min(s) for t, s in series.items()}
    en_defaut = {t: d for t, d in premiere_cotation.items() if d > date_debut}
    if en_defaut:
        detail = ", ".join(f"{t} depuis le {d}" for t, d in sorted(en_defaut.items()))
        raise HistoriqueInsuffisant(
            f"historique insuffisant pour démarrer le {date_debut} : {detail}"
        )

    # Toutes les lignes ont une cotation avant ou à `date_debut` : la date de
    # départ effective est la plus tardive des "premier jour coté >= date_debut"
    # — ça aligne tout le monde sur un même jour de calendrier réel.
    date_debut_eff = max(debuts_par_ticker.values())

    dernieres_par_ticker = {t: max(s) for t, s in series.items()}
    date_fin_eff = date_fin or min(dernieres_par_ticker.values())
    if any(d < date_fin_eff for d in dernieres_par_ticker.values()):
        manquants = {t: d for t, d in dernieres_par_ticker.items() if d < date_fin_eff}
        detail = ", ".join(f"{t} s'arrête au {d}" for t, d in sorted(manquants.items()))
        raise HistoriqueInsuffisant(
            f"pas de cotation commune jusqu'au {date_fin_eff} : {detail}"
        )

    # Axe de dates = union des jours réellement cotés par au moins une ligne,
    # sur la période. Les autres lignes sont portées en report (dernier cours
    # connu) sur ces jours — deux places (Paris, Amsterdam, Xetra) n'ont pas
    # toujours exactement le même calendrier de jours fériés.
    axe = sorted(
        {d for s in series.values() for d in s if date_debut_eff <= d <= date_fin_eff}
    )

    capital_investi = {t: capital_initial * p / 100 for t, p in allocation.items()}

    # Cours de départ « au plus tard à date_debut_eff », pas « exactement à
    # date_debut_eff » : rien ne garantit qu'une ligne cote précisément ce
    # jour-là même si `date_debut_eff` a été choisi comme date pivot (c'est le
    # premier jour coté d'UNE des lignes, pas forcément de toutes). Sans ce
    # report, une ligne cotée sur un calendrier de jours fériés différent
    # ferait planter la simulation sur un KeyError.
    dernier_connu: dict[str, float] = {}
    for t in allocation:
        jours_avant = [d for d in series[t] if d <= date_debut_eff]
        dernier_connu[t] = series[t][max(jours_avant)]
    prix_debut = dict(dernier_connu)

    serie_totale: list[tuple[str, float]] = []
    for jour in axe:
        for t in allocation:
            if jour in series[t]:
                dernier_connu[t] = series[t][jour]
        total_jour = sum(
            capital_investi[t] * dernier_connu[t] / prix_debut[t] for t in allocation
        )
        serie_totale.append((jour, total_jour))

    prix_fin = {t: dernier_connu[t] for t in allocation}
    detail = [
        DetailInstrument(
            ticker=t,
            poids_pct=allocation[t],
            prix_debut=prix_debut[t],
            prix_fin=prix_fin[t],
            capital_investi=capital_investi[t],
            valeur_finale=capital_investi[t] * prix_fin[t] / prix_debut[t],
        )
        for t in allocation
    ]

    return ResultatSimulation(
        date_debut=date_debut_eff,
        date_fin=date_fin_eff,
        capital_initial=sum(capital_investi.values()),
        valeur_finale=sum(d.valeur_finale for d in detail),
        detail=detail,
        serie=serie_totale,
    )
