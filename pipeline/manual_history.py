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
Colonne `adjusted_close` facultative : à défaut, `close` est repris tel quel.
Séparateur `,` ou `;`, BOM Excel toléré — comme pour le catalogue.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path

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
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ManualHistoryError(
        f"{path.name} ligne {line_no} : date '{raw}' illisible "
        f"(attendu 2015-01-02 ou 02/01/2015)."
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


def load_file(path: Path) -> list[tuple[date, float, float]]:
    """Lit un fichier de cours manuels. Renvoie [(date, close, adjusted_close)]."""
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

    rows: list[tuple[date, float, float]] = []
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
        adjusted = close
        if "adjusted_close" in columns:
            raw_adj = (row.get(columns["adjusted_close"]) or "").strip()
            if raw_adj:
                adjusted = _parse_number(raw_adj, path, line_no, "adjusted_close")
        rows.append((day, close, adjusted))

    if not rows:
        raise ManualHistoryError(f"{path.name} ne contient aucune cotation.")
    return sorted(rows)


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
           rows: list[tuple[date, float, float]]) -> list[str]:
    """Contrôle la cohérence du raccord. Renvoie la liste des avertissements."""
    warnings: list[str] = []

    # 1. Cours ajusté : le raccord n'est neutre que si l'instrument ne distribue
    #    rien sur la période connue de Yahoo.
    distributes = cx.execute(
        """SELECT COUNT(*) n FROM prices_daily
           WHERE instrument_id = ? AND source = 'yahoo'
             AND ABS(close - adjusted_close) > 0.005""",
        (instrument_id,),
    ).fetchone()["n"]
    if distributes:
        warnings.append(
            f"{ticker} : la partie Yahoo distingue cours brut et cours ajusté sur "
            f"{distributes} jours (l'ETF distribue des dividendes). Greffer des "
            f"cours BRUTS sous-estimera le rendement — renseignez la colonne "
            f"'adjusted_close' ou renoncez au raccord pour cette ligne."
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
