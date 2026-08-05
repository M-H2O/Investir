"""Complément manuel d'historique, pour les cours que Yahoo ne fournit pas.

Plusieurs fonds existent depuis dix ans alors que Yahoo n'en publie que deux
(fusions Lyxor/Amundi, changements de ligne de cotation). Les cours manquants
se trouvent chez l'émetteur ou un courtier : ce module permet de les saisir à
la main dans `data/history_manual/<TICKER>.csv`.

Règle de préséance
------------------
Le manuel **ne remplace jamais** une cotation Yahoo : il ne comble que les
dates absentes. Si Yahoo étend un jour son historique vers le passé, ses
valeurs reprennent la main automatiquement. En revanche, corriger votre fichier
et relancer met bien à jour les lignes que VOUS aviez saisies.

Deux pièges, traités par les vérifications de `verify()`
--------------------------------------------------------
1. Le simulateur calcule sur le cours AJUSTÉ (dividendes réinvestis). Les cours
   publiés par un courtier sont en général BRUTS. Pour un ETF capitalisant les
   deux coïncident et le raccord est sûr ; pour un ETF distribuant, greffer du
   brut sur de l'ajusté sous-estime le rendement de tout l'écart de dividendes.
2. Une série venue d'une autre place ou d'une VL au lieu du cours de clôture
   crée une marche à la jonction. Faire chevaucher volontairement quelques mois
   avec la période Yahoo permet de le mesurer plutôt que de l'espérer.

Format accepté
--------------
    date,close
    2015-01-02,142.35

`date` en ISO (2015-01-02) ou au format français (02/01/2015).
`close` avec un point ou une virgule décimale, espaces tolérés.
Deux colonnes facultatives :
  `adjusted_close`  si vous disposez déjà d'une série ajustée — prioritaire ;
  `dividend`        montant détaché ce jour-là, pour un ETF DISTRIBUANT. Le
                    pipeline calcule alors l'ajustement lui-même et le raccorde
                    à l'échelle de Yahoo (voir `resolve_adjusted`).
À défaut des deux, `close` est repris tel quel — correct pour un ETF
capitalisant, faux pour un distribuant.
Séparateur `,` ou `;`, BOM Excel toléré — comme pour le catalogue.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

MANUAL_DIR = Path(__file__).parent.parent / "data" / "history_manual"

# Un fichier dont le nom commence par _ ou . est un modèle ou un brouillon.
IGNORED_PREFIXES = ("_", ".")

SOURCE_PREFIX = "manuel"

# Au-delà de cet écart relatif sur une date couverte par les deux sources, la
# série manuelle ne décrit visiblement pas la même chose que Yahoo.
OVERLAP_TOLERANCE_PCT = 1.0

# Marche admise à la jonction des deux séries, quand elles ne se chevauchent
# pas. Un ETF actions bouge rarement de plus de ça en un jour de bourse.
SPLICE_TOLERANCE_PCT = 5.0


class ManualHistoryError(Exception):
    """Fichier inexploitable — on refuse d'insérer des cours qu'on n'a pas compris."""


def _parse_date(raw: str, path: Path, line_no: int) -> date:
    text = raw.strip()
    # Les exports de cours accolent très souvent une heure — « 05/08/2016 00:00 »
    # chez un courtier, « 2016-08-05T00:00:00 » en ISO. Elle n'apporte rien sur
    # une série quotidienne : on ne garde que la partie date.
    head = text.split()[0] if text.split() else text
    head = head.split("T")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    raise ManualHistoryError(
        f"{path.name} ligne {line_no} : date '{raw}' illisible "
        f"(attendu 2015-01-02 ou 02/01/2015, éventuellement suivi d'une heure)."
    )


def _parse_number(raw: str, path: Path, line_no: int, column: str) -> float:
    # Espaces fines/insécables des séparateurs de milliers copiés depuis un site.
    text = raw.replace(" ", "").replace(" ", "").replace(" ", "").strip()
    if not text:
        raise ManualHistoryError(f"{path.name} ligne {line_no} : '{column}' est vide.")
    # Virgule décimale française. Un point ET une virgule => le point sépare
    # les milliers (1.234,56), on le retire avant de convertir la virgule.
    if "," in text:
        text = text.replace(".", "") if "." in text else text
        text = text.replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        raise ManualHistoryError(
            f"{path.name} ligne {line_no} : '{column}' = '{raw}' n'est pas un nombre."
        ) from None
    if value <= 0:
        raise ManualHistoryError(
            f"{path.name} ligne {line_no} : '{column}' = {value}, un cours doit être positif."
        )
    return value


class ManualQuote(NamedTuple):
    day: date
    close: float
    adjusted: float | None   # None = à calculer (voir resolve_adjusted)
    dividend: float          # 0.0 si aucun détachement ce jour-là


def load_file(path: Path) -> list[ManualQuote]:
    """Lit un fichier de cours manuels.

    `adjusted` vaut None quand la colonne `adjusted_close` est absente ou vide :
    c'est `resolve_adjusted` qui le calculera, à partir des dividendes si la
    colonne `dividend` est renseignée.
    """
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ManualHistoryError(f"{path.name} est vide.")

    header = text.splitlines()[0]
    delimiter = ";" if header.count(";") > header.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ManualHistoryError(f"{path.name} : ligne d'en-tête absente.")

    columns = {(h or "").strip().lower(): h for h in reader.fieldnames}
    for required in ("date", "close"):
        if required not in columns:
            raise ManualHistoryError(
                f"{path.name} : colonne '{required}' absente "
                f"(trouvé : {', '.join(reader.fieldnames)})."
            )

    rows: list[ManualQuote] = []
    seen: dict[date, int] = {}
    for line_no, row in enumerate(reader, start=2):
        raw_date = (row.get(columns["date"]) or "").strip()
        if not raw_date:
            if any((v or "").strip() for v in row.values() if isinstance(v, str)):
                raise ManualHistoryError(
                    f"{path.name} ligne {line_no} : date vide sur une ligne remplie."
                )
            continue

        day = _parse_date(raw_date, path, line_no)
        if day in seen:
            raise ManualHistoryError(
                f"{path.name} ligne {line_no} : date {day} déjà présente "
                f"ligne {seen[day]}."
            )
        seen[day] = line_no

        if day > date.today():
            raise ManualHistoryError(
                f"{path.name} ligne {line_no} : date {day} dans le futur."
            )

        close = _parse_number(row[columns["close"]], path, line_no, "close")

        adjusted = None
        if "adjusted_close" in columns:
            raw_adj = (row.get(columns["adjusted_close"]) or "").strip()
            if raw_adj:
                adjusted = _parse_number(raw_adj, path, line_no, "adjusted_close")

        dividend = 0.0
        if "dividend" in columns:
            raw_div = (row.get(columns["dividend"]) or "").strip()
            # 0 est une valeur légitime ici (pas de détachement), contrairement
            # à un cours : on ne passe donc pas par _parse_number.
            if raw_div and raw_div not in {"0", "0.0", "0,0", "0,00"}:
                dividend = _parse_number(raw_div, path, line_no, "dividend")

        rows.append(ManualQuote(day, close, adjusted, dividend))

    if not rows:
        raise ManualHistoryError(f"{path.name} ne contient aucune cotation.")
    return sorted(rows)


def yahoo_scale_factor(cx, instrument_id: int) -> tuple[float, str | None]:
    """Facteur d'ajustement cumulé de Yahoo à sa plus ancienne cotation.

    Yahoo publie `adjusted_close = close × F(t)`, où F(t) rassemble tous les
    détachements postérieurs à t. À sa date la plus ancienne T0, le rapport
    adj/close DONNE donc F(T0) : tous les dividendes versés depuis T0. C'est
    exactement le facteur qui manque à un cours brut plus ancien que T0.
    """
    row = cx.execute(
        """SELECT price_date, close, adjusted_close FROM prices_daily
           WHERE instrument_id = ? AND source = 'yahoo'
             AND close IS NOT NULL AND adjusted_close IS NOT NULL AND close > 0
           ORDER BY price_date LIMIT 1""",
        (instrument_id,),
    ).fetchone()
    if row is None:
        return 1.0, None
    return row["adjusted_close"] / row["close"], row["price_date"]


def resolve_adjusted(cx, instrument_id: int,
                     quotes: list[ManualQuote]) -> list[tuple[date, float, float]]:
    """Transforme des cours BRUTS en cours ajustés sur l'échelle de Yahoo.

    Un ETF distribuant détache des dividendes : son cours brut décroche à chaque
    versement, alors que le porteur, lui, a touché l'argent. Le cours ajusté
    rétablit cette continuité en rétro-appliquant les détachements. Sans ça,
    greffer du brut sur de l'ajusté sous-estime le rendement de tout le
    cumul des dividendes.

    Deux facteurs se composent, et c'est ce chaînage qui rend le résultat exact
    plutôt qu'approché :
      1. les détachements SURVENUS PENDANT la période saisie, fournis par vous
         dans la colonne `dividend` ;
      2. tous ceux survenus DEPUIS, que Yahoo connaît déjà — c'est
         `yahoo_scale_factor`.

    Une valeur explicite en colonne `adjusted_close` a toujours priorité : si
    vous disposez d'une série déjà ajustée, on n'y touche pas.
    """
    scale, _ = yahoo_scale_factor(cx, instrument_id)

    resolved: list[tuple[date, float, float]] = [None] * len(quotes)  # type: ignore[list-item]
    factor = scale
    for i in range(len(quotes) - 1, -1, -1):
        q = quotes[i]
        adjusted = q.adjusted if q.adjusted is not None else q.close * factor
        resolved[i] = (q.day, q.close, adjusted)
        # Le détachement du jour i fait décrocher le cours : il ne s'applique
        # qu'aux dates ANTÉRIEURES, d'où la mise à jour après affectation.
        if q.dividend > 0 and i > 0:
            previous_close = quotes[i - 1].close
            if previous_close > 0:
                factor *= max(0.0, 1 - q.dividend / previous_close)
    return resolved


def available_files(directory: Path | None = None) -> dict[str, Path]:
    """{ticker: fichier} pour chaque fichier de cours manuels présent."""
    directory = directory or MANUAL_DIR
    if not directory.exists():
        return {}
    return {
        p.stem.upper(): p
        for p in sorted(directory.glob("*.csv"))
        if not p.name.startswith(IGNORED_PREFIXES)
    }


def insert(cx, instrument_id: int, rows: list[tuple[date, float, float]],
           label: str) -> int:
    """Insère les cours manuels SANS jamais écraser une cotation Yahoo.

    La clause WHERE du DO UPDATE est le cœur de la règle de préséance : on ne
    met à jour que les lignes déjà marquées comme manuelles, ce qui permet de
    corriger son fichier sans jamais écraser la source de référence.
    """
    cx.executemany(
        """
        INSERT INTO prices_daily
               (instrument_id, price_date, close, adjusted_close, source, ingested_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (instrument_id, price_date) DO UPDATE SET
               close          = excluded.close,
               adjusted_close = excluded.adjusted_close,
               source         = excluded.source,
               ingested_at    = excluded.ingested_at
        WHERE prices_daily.source LIKE 'manuel%'
        """,
        [(instrument_id, d.isoformat(), c, a, f"{SOURCE_PREFIX}:{label}")
         for d, c, a in rows],
    )
    return len(rows)


def verify(cx, instrument_id: int, ticker: str,
           rows: list[tuple[date, float, float]],
           compensated: bool = False) -> list[str]:
    """Contrôle la cohérence du raccord. Renvoie la liste des avertissements.

    `compensated` indique que le fichier fournit de quoi rétablir l'ajustement
    (colonne `dividend` ou `adjusted_close`) : sans ça, on ne saurait pas
    distinguer un raccord correctement compensé d'un raccord de cours bruts.
    """
    warnings: list[str] = []

    # 1. Cours ajusté : le raccord n'est neutre que si l'instrument ne distribue
    #    rien, OU si vous avez fourni de quoi rétablir l'ajustement.
    distributes = cx.execute(
        """SELECT COUNT(*) n FROM prices_daily
           WHERE instrument_id = ? AND source = 'yahoo'
             AND ABS(close - adjusted_close) > 0.005""",
        (instrument_id,),
    ).fetchone()["n"]
    if distributes:
        if compensated:
            scale, since = yahoo_scale_factor(cx, instrument_id)
            warnings.append(
                f"{ticker} : ✓ ETF distribuant, ajustement reconstitué à partir de "
                f"vos dividendes puis raccordé au facteur Yahoo "
                f"({scale:.4f} depuis le {since})."
            )
        else:
            warnings.append(
                f"{ticker} : la partie Yahoo distingue cours brut et cours ajusté sur "
                f"{distributes} jours — cet ETF DISTRIBUE des dividendes. Des cours "
                f"bruts sous-estimeront le rendement : ajoutez une colonne 'dividend' "
                f"(montant détaché, 0 ailleurs) ou 'adjusted_close'."
            )

    manual_by_date = {d: a for d, _, a in rows}

    # 2. Chevauchement : la comparaison directe est la vérification la plus forte.
    overlap = cx.execute(
        """SELECT price_date, adjusted_close FROM prices_daily
           WHERE instrument_id = ? AND source = 'yahoo'
             AND price_date BETWEEN ? AND ?""",
        (instrument_id, rows[0][0].isoformat(), rows[-1][0].isoformat()),
    ).fetchall()
    compared = [
        (r["price_date"],
         abs(manual_by_date[date.fromisoformat(r["price_date"])] - r["adjusted_close"])
         / r["adjusted_close"] * 100)
        for r in overlap
        if date.fromisoformat(r["price_date"]) in manual_by_date and r["adjusted_close"]
    ]
    if compared:
        worst_date, worst = max(compared, key=lambda x: x[1])
        if worst > OVERLAP_TOLERANCE_PCT:
            warnings.append(
                f"{ticker} : sur les {len(compared)} dates couvertes par les deux "
                f"sources, l'écart atteint {worst:.1f} % (le {worst_date}). Vos cours "
                f"ne décrivent probablement pas la même ligne de cotation."
            )
        else:
            warnings.append(
                f"{ticker} : ✓ recoupement sur {len(compared)} dates, écart max "
                f"{worst:.2f} % — la série manuelle concorde avec Yahoo."
            )

    # 3. Sans chevauchement, on ne peut que regarder la marche à la jonction.
    else:
        first_yahoo = cx.execute(
            """SELECT price_date, adjusted_close FROM prices_daily
               WHERE instrument_id = ? AND source = 'yahoo' AND price_date > ?
               ORDER BY price_date LIMIT 1""",
            (instrument_id, rows[-1][0].isoformat()),
        ).fetchone()
        if first_yahoo and first_yahoo["adjusted_close"]:
            gap_days = (date.fromisoformat(first_yahoo["price_date"]) - rows[-1][0]).days
            jump = abs(rows[-1][2] - first_yahoo["adjusted_close"]) \
                / first_yahoo["adjusted_close"] * 100
            if gap_days <= 7 and jump > SPLICE_TOLERANCE_PCT:
                warnings.append(
                    f"{ticker} : marche de {jump:.1f} % entre votre dernier cours "
                    f"({rows[-1][0]}) et le premier cours Yahoo "
                    f"({first_yahoo['price_date']}), à {gap_days} jour(s) d'écart. "
                    f"Faites chevaucher quelques mois pour lever le doute."
                )
    return warnings
