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

from catalogue import all_instruments

# La sortie contient des caractères non-ASCII (encadrés, flèches, accents). Sous
# Windows, une console ou un pipe en cp1252 ferait planter le script au moment
# d'AFFICHER le bilan, après le travail utile — on force donc l'UTF-8.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):              # flux déjà redirigé
        pass


ROOT = Path(__file__).parent
DEFAULT_DB = ROOT / "boussole.db"
DEFAULT_OUTPUT = ROOT.parent / "data" / "cours.json"

# 4 décimales : l'erreur relative retombe sous 1e-6, ce qui garantit que le
# calcul JS retrouve les mêmes valeurs que le moteur Python de référence.
# À 2 décimales l'écart devenait visible sur les rapports de prix.
DECIMALS = 4


def export(db: Path, sortie: Path) -> dict:
    if not db.exists():
        raise SystemExit(f"Base introuvable : {db}\nLancez d'abord : python ingest.py --init --full")

    # L'export suit le CATALOGUE, pas la base : une ligne retirée de
    # data/tickers.csv (ou passée à active=non) disparaît du site à la première
    # régénération, sans qu'il faille purger la base. L'historique reste stocké,
    # donc réactiver la ligne plus tard ne coûte aucun re-téléchargement.
    catalogue = {i.yahoo: i for i in all_instruments()}

    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row

    rows = cx.execute(
        """SELECT i.ticker, i.name, i.isin, i.currency, i.yahoo_symbol,
                  p.price_date, p.adjusted_close, p.source
           FROM prices_daily p JOIN instruments i USING(instrument_id)
           WHERE p.adjusted_close IS NOT NULL
           ORDER BY p.price_date"""
    ).fetchall()
    cx.close()

    rows = [r for r in rows if r["yahoo_symbol"] in catalogue]
    absents = [y for y in catalogue if not any(r["yahoo_symbol"] == y for r in rows)]
    if absents:
        print(f"  ⚠  au catalogue mais sans cours en base : {', '.join(absents)}"
              f"\n     -> lancez `python ingest.py` pour les récupérer\n")

    # Le simulateur additionne des montants sans convertir : mélanger des lignes
    # cotées dans des devises différentes produirait un total dénué de sens. On
    # le signale ici, au moment où la ligne entre dans le site.
    devises = {r["ticker"]: r["currency"] for r in rows}
    hors_euro = {t: c for t, c in devises.items() if (c or "").upper() != "EUR"}
    if hors_euro:
        detail = ", ".join(f"{t} en {c}" for t, c in sorted(hors_euro.items()))
        print(f"  ⚠  DEVISE : {detail}.\n"
              f"     Le simulateur ne convertit pas les devises — il avertira à "
              f"l'écran si l'utilisateur mélange.\n"
              f"     Préférez une ligne de cotation en euros du même fonds "
              f"(recherche par ISIN).\n")

    if not rows:
        raise SystemExit("Aucune cotation en base — lancez d'abord ingest.py")

    dates = sorted({r["price_date"] for r in rows})
    index_of = {d: n for n, d in enumerate(dates)}

    raw: dict[str, dict] = {}
    for r in rows:
        e = raw.setdefault(
            r["ticker"],
            {"nom": r["name"], "isin": r["isin"], "devise": r["currency"],
             "points": {}, "manual": []},
        )
        e["points"][index_of[r["price_date"]]] = round(r["adjusted_close"], DECIMALS)
        if (r["source"] or "").startswith("manuel"):
            e["manual"].append(r["price_date"])

    instruments = {}
    for ticker, e in sorted(raw.items()):
        positions = sorted(e["points"])
        i0, i1 = positions[0], positions[-1]
        entry = {
            "nom": e["nom"],
            "isin": e["isin"],
            "devise": e["devise"],
            "i0": i0,
            "prix": [e["points"].get(i) for i in range(i0, i1 + 1)],
        }
        # Bornes de la partie saisie à la main : le site doit pouvoir le dire à
        # l'utilisateur plutôt que de présenter toute la courbe comme sourcée
        # de la même façon.
        if e["manual"]:
            entry["manuel"] = {"de": min(e["manual"]), "a": max(e["manual"]),
                               "jours": len(e["manual"])}
        instruments[ticker] = entry

    payload = {
        "genere_le": date.today().isoformat(),
        "source": "Yahoo Finance (cours ajustes) via pipeline/ingest.py",
        "resolution": "quotidienne",
        "dates": dates,
        "instruments": instruments,
    }

    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--sortie", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args(argv)

    payload = export(args.db, args.sortie)
    size_kb = args.sortie.stat().st_size / 1024

    print(f"\n{args.sortie}")
    print(f"  {len(payload['instruments'])} instruments · {len(payload['dates'])} dates "
          f"· {size_kb:.0f} Ko")
    print(f"  periode : {payload['dates'][0]} -> {payload['dates'][-1]}\n")
    for t, e in payload["instruments"].items():
        start = payload["dates"][e["i0"]]
        gaps = sum(1 for v in e["prix"] if v is None)
        print(f"  {t:<6} depuis {start}  {len(e['prix']):>5} points"
              + (f"  ({gaps} jours non cotes, reportes)" if gaps else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
