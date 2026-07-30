#!/usr/bin/env python3
"""Export de la base vers le JSON statique que consomme le site.

Le site est un fichier HTML sans backend : il ne peut pas ouvrir SQLite. Cette
étape est donc le pont entre le pipeline et l'interface — à relancer À LA MAIN
après chaque `ingest.py`, sinon le simulateur affiche des cours périmés.

    python ingest.py            # 1. rafraîchit la base
    python export_json.py       # 2. régénère ../data/cours.json

Format produit (compact volontairement — 20 ETF x ~4500 jours) :

    {
      "genere_le":  "2026-07-30",
      "dates":      ["2009-01-02", ...],        # axe partagé, trié
      "instruments": {
        "IWDA": {
          "nom": "...", "isin": "...", "devise": "EUR",
          "i0":   142,                          # index dans `dates` du 1er cours
          "prix": [88.8, 88.95, null, ...]      # dense à partir de i0
        }
      }
    }

`prix` est dense à partir de `i0` et peut contenir des `null` : deux places de
cotation (Paris, Amsterdam, Xetra) n'ont pas les mêmes jours fériés. Le moteur
JS reporte alors le dernier cours connu, exactement comme portefeuille.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

RACINE = Path(__file__).parent
DB_DEFAUT = RACINE / "boussole.db"
SORTIE_DEFAUT = RACINE.parent / "data" / "cours.json"

# 4 décimales : l'erreur relative retombe sous 1e-6, ce qui garantit que le
# calcul JS retrouve les mêmes valeurs que le moteur Python de référence.
# À 2 décimales l'écart devenait visible sur les rapports de prix.
DECIMALES = 4


def exporter(db: Path, sortie: Path) -> dict:
    if not db.exists():
        raise SystemExit(f"Base introuvable : {db}\nLancez d'abord : python ingest.py --init --full")

    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row

    lignes = cx.execute(
        """SELECT i.ticker, i.name, i.isin, i.currency, p.price_date, p.adjusted_close
           FROM prices_daily p JOIN instruments i USING(instrument_id)
           WHERE p.adjusted_close IS NOT NULL
           ORDER BY p.price_date"""
    ).fetchall()
    cx.close()

    if not lignes:
        raise SystemExit("Aucune cotation en base — lancez d'abord ingest.py")

    dates = sorted({r["price_date"] for r in lignes})
    index_de = {d: n for n, d in enumerate(dates)}

    brut: dict[str, dict] = {}
    for r in lignes:
        e = brut.setdefault(
            r["ticker"],
            {"nom": r["name"], "isin": r["isin"], "devise": r["currency"], "points": {}},
        )
        e["points"][index_de[r["price_date"]]] = round(r["adjusted_close"], DECIMALES)

    instruments = {}
    for ticker, e in sorted(brut.items()):
        positions = sorted(e["points"])
        i0, i1 = positions[0], positions[-1]
        instruments[ticker] = {
            "nom": e["nom"],
            "isin": e["isin"],
            "devise": e["devise"],
            "i0": i0,
            "prix": [e["points"].get(i) for i in range(i0, i1 + 1)],
        }

    charge = {
        "genere_le": date.today().isoformat(),
        "source": "Yahoo Finance (cours ajustes) via pipeline/ingest.py",
        "resolution": "quotidienne",
        "dates": dates,
        "instruments": instruments,
    }

    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(
        json.dumps(charge, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
    )
    return charge


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DB_DEFAUT)
    p.add_argument("--sortie", type=Path, default=SORTIE_DEFAUT)
    args = p.parse_args(argv)

    charge = exporter(args.db, args.sortie)
    poids = args.sortie.stat().st_size / 1024

    print(f"\n{args.sortie}")
    print(f"  {len(charge['instruments'])} instruments · {len(charge['dates'])} dates "
          f"· {poids:.0f} Ko")
    print(f"  periode : {charge['dates'][0]} -> {charge['dates'][-1]}\n")
    for t, e in charge["instruments"].items():
        debut = charge["dates"][e["i0"]]
        trous = sum(1 for v in e["prix"] if v is None)
        print(f"  {t:<6} depuis {debut}  {len(e['prix']):>5} points"
              + (f"  ({trous} jours non cotes, reportes)" if trous else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
