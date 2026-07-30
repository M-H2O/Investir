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


class SimulationError(Exception):
    """Base commune : toute erreur remontée par ce module en hérite."""


class InvalidWeights(SimulationError):
    pass


class UnknownTicker(SimulationError):
    pass


class InsufficientHistory(SimulationError):
    pass


# Tolérance sur la somme des poids : absorbe l'arrondi flottant d'une UI à
# sliders (33.33 + 33.33 + 33.34) sans laisser passer une vraie erreur de
# saisie (30 + 30 + 30 = 90 doit échouer).
WEIGHT_TOLERANCE_PCT = 0.1


@dataclass
class HoldingResult:
    ticker: str
    weight_pct: float
    start_price: float
    end_price: float
    invested: float
    final_value: float

    @property
    def gain_pct(self) -> float:
        return (self.final_value / self.invested - 1) * 100


@dataclass
class SimulationResult:
    start_date: str          # dates effectivement utilisées : elles peuvent
    end_date: str            # différer des dates demandées si celles-ci
    initial_capital: float   # n'étaient pas des jours cotés
    final_value: float
    holdings: list[HoldingResult]
    # (date, valeur totale) pour chaque jour coté de la période — c'est la
    # série que consomme un curseur temporel dans l'interface.
    series: list[tuple[str, float]] = field(repr=False)

    @property
    def gain_absolute(self) -> float:
        return self.final_value - self.initial_capital

    @property
    def gain_pct(self) -> float:
        return (self.final_value / self.initial_capital - 1) * 100


def _validate_weights(allocation: dict[str, float]) -> None:
    if not allocation:
        raise InvalidWeights("l'allocation est vide")
    total = sum(allocation.values())
    if abs(total - 100.0) > WEIGHT_TOLERANCE_PCT:
        raise InvalidWeights(
            f"les poids totalisent {total:.2f} %, pas 100 % "
            f"(tolérance ±{WEIGHT_TOLERANCE_PCT} point)"
        )
    negative = [t for t, w in allocation.items() if w < 0]
    if negative:
        raise InvalidWeights(f"poids négatif pour : {', '.join(negative)}")


def _instrument_id(cx: sqlite3.Connection, ticker: str) -> int:
    row = cx.execute(
        "SELECT instrument_id FROM instruments WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row is None:
        raise UnknownTicker(f"« {ticker} » n'existe pas dans le catalogue")
    return row["instrument_id"]


def _price_series(cx: sqlite3.Connection, instrument_id: int) -> dict[str, float]:
    """Toutes les cotations d'un instrument, sous forme {date: adjusted_close}."""
    rows = cx.execute(
        """SELECT price_date, adjusted_close FROM prices_daily
           WHERE instrument_id = ? AND adjusted_close IS NOT NULL
           ORDER BY price_date""",
        (instrument_id,),
    ).fetchall()
    return {r["price_date"]: r["adjusted_close"] for r in rows}


def simulate(
    cx: sqlite3.Connection,
    allocation: dict[str, float],
    initial_capital: float,
    start_date: str,
    end_date: str | None = None,
) -> SimulationResult:
    """Simule un investissement unique de `initial_capital` réparti selon
    `allocation` ({ticker: poids en %}), à partir de `start_date`.

    `end_date` par défaut = la dernière date où TOUTES les lignes de
    l'allocation ont une cotation — jamais une date où l'une d'elles serait
    silencieusement absente.

    Lève `InsufficientHistory` plutôt que de démarrer plus tard que demandé
    sans le dire : si on simule « il y a 10 ans » avec une ligne qui n'existe
    que depuis 2 ans, l'appelant doit le savoir, pas le déduire d'un résultat
    qui commence ailleurs que prévu.
    """
    _validate_weights(allocation)

    series: dict[str, dict[str, float]] = {}
    for ticker in allocation:
        series[ticker] = _price_series(cx, _instrument_id(cx, ticker))
        if not series[ticker]:
            raise InsufficientHistory(f"« {ticker} » n'a aucune cotation en base")

    # Première date cotée >= start_date, pour chaque ligne.
    first_on_or_after: dict[str, str] = {}
    for ticker, quotes in series.items():
        available = sorted(d for d in quotes if d >= start_date)
        if not available:
            raise InsufficientHistory(
                f"« {ticker} » n'a aucune cotation à partir du {start_date}"
            )
        first_on_or_after[ticker] = available[0]

    earliest = {t: min(q) for t, q in series.items()}
    too_recent = {t: d for t, d in earliest.items() if d > start_date}
    if too_recent:
        detail = ", ".join(f"{t} depuis le {d}" for t, d in sorted(too_recent.items()))
        raise InsufficientHistory(
            f"historique insuffisant pour démarrer le {start_date} : {detail}"
        )

    # Toutes les lignes ont une cotation avant ou à `start_date` : la date de
    # départ effective est la plus tardive des « premier jour coté >= start_date »
    # — ça aligne tout le monde sur un même jour de calendrier réel.
    effective_start = max(first_on_or_after.values())

    latest = {t: max(q) for t, q in series.items()}
    effective_end = end_date or min(latest.values())
    if any(d < effective_end for d in latest.values()):
        missing = {t: d for t, d in latest.items() if d < effective_end}
        detail = ", ".join(f"{t} s'arrête au {d}" for t, d in sorted(missing.items()))
        raise InsufficientHistory(
            f"pas de cotation commune jusqu'au {effective_end} : {detail}"
        )

    # Axe de dates = union des jours réellement cotés par au moins une ligne,
    # sur la période. Les autres lignes sont portées en report (dernier cours
    # connu) sur ces jours — deux places (Paris, Amsterdam, Xetra) n'ont pas
    # toujours exactement le même calendrier de jours fériés.
    axis = sorted(
        {d for q in series.values() for d in q if effective_start <= d <= effective_end}
    )

    invested = {t: initial_capital * w / 100 for t, w in allocation.items()}

    # Cours de départ « au plus tard à effective_start », pas « exactement à
    # effective_start » : rien ne garantit qu'une ligne cote précisément ce
    # jour-là même si `effective_start` a été choisi comme date pivot (c'est le
    # premier jour coté d'UNE des lignes, pas forcément de toutes). Sans ce
    # report, une ligne cotée sur un calendrier de jours fériés différent
    # ferait planter la simulation sur un KeyError.
    last_known: dict[str, float] = {}
    for ticker in allocation:
        before = [d for d in series[ticker] if d <= effective_start]
        last_known[ticker] = series[ticker][max(before)]
    start_price = dict(last_known)

    value_series: list[tuple[str, float]] = []
    for day in axis:
        for ticker in allocation:
            if day in series[ticker]:
                last_known[ticker] = series[ticker][day]
        value_series.append(
            (day, sum(invested[t] * last_known[t] / start_price[t] for t in allocation))
        )

    end_price = {t: last_known[t] for t in allocation}
    holdings = [
        HoldingResult(
            ticker=t,
            weight_pct=allocation[t],
            start_price=start_price[t],
            end_price=end_price[t],
            invested=invested[t],
            final_value=invested[t] * end_price[t] / start_price[t],
        )
        for t in allocation
    ]

    return SimulationResult(
        start_date=effective_start,
        end_date=effective_end,
        initial_capital=sum(invested.values()),
        final_value=sum(h.final_value for h in holdings),
        holdings=holdings,
        series=value_series,
    )
