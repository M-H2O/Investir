#!/usr/bin/env python3
"""Ingestion des cours quotidiens depuis Yahoo Finance vers la base SQLite.

Le script est idempotent : le relancer ne duplique rien et met à jour les lignes
déjà présentes (Yahoo révise son historique après coup — splits, dividendes).

    python ingest.py --init                 # crée le schéma + charge le catalogue
    python ingest.py                        # incrémental (défaut)
    python ingest.py --full                 # rejoue tout l'historique disponible
    python ingest.py --tickers CSPX IWDA    # limite à quelques instruments
    python ingest.py --dry-run              # montre ce qui serait fait, n'écrit rien
    python ingest.py --fx                   # ajoute les paires de change
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from catalogue import FX_PAIRS, Instrument, all_instruments

ROOT = Path(__file__).parent
DEFAULT_DB = ROOT / "boussole.db"
SCHEMA = ROOT / "schema.sql"
SOURCE = "yahoo"

# On refetch quelques jours déjà connus à chaque passe : Yahoo corrige son
# historique récent a posteriori, et l'upsert absorbe la reprise sans doublon.
OVERLAP_DAYS = 7

# Les repères `yahoo_depuis` du catalogue proviennent d'une requête mensuelle :
# ils précèdent la première barre quotidienne de quelques jours. On ne signale
# donc qu'un écart franc, pas ce décalage de calendrier.
HISTORY_TOLERANCE_DAYS = 31

MAX_ATTEMPTS = 3
PAUSE_BETWEEN_TICKERS = 0.4   # secondes — on reste poli avec une API non contractuelle

log = logging.getLogger("ingest")


def _configure_yfinance() -> None:
    """Fait remonter les erreurs yfinance au lieu de les avaler.

    Par défaut yfinance masque les exceptions et renvoie un DataFrame vide, ce
    qui ferait passer une panne réseau pour « aucune cotation ». L'ancien
    `raise_errors=True` de `history()` est déprécié depuis la 1.5 ; on garde un
    repli silencieux pour rester compatible avec les 0.2.x.
    """
    try:
        yf.config.debug.hide_exceptions = False
    except AttributeError:                            # yfinance < 1.5
        log.debug("yf.config indisponible — comportement d'erreurs par défaut")


_configure_yfinance()


# ---------------------------------------------------------------------
#  Base
# ---------------------------------------------------------------------
def open_db(chemin: Path) -> sqlite3.Connection:
    cx = sqlite3.connect(chemin)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    return cx


def init_schema(cx: sqlite3.Connection) -> None:
    cx.executescript(SCHEMA.read_text(encoding="utf-8"))
    cx.commit()
    log.info("Schéma appliqué (%s)", SCHEMA.name)


def sync_catalogue(cx: sqlite3.Connection, instruments: list[Instrument]) -> None:
    """Insère ou met à jour la dimension. Ne touche jamais aux prix."""
    for i in instruments:
        cx.execute(
            """
            INSERT INTO instruments
                   (ticker, yahoo_symbol, isin, name, asset_type, currency, exchange)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ticker, exchange) DO UPDATE SET
                   yahoo_symbol = excluded.yahoo_symbol,
                   isin         = excluded.isin,
                   name         = excluded.name,
                   asset_type   = excluded.asset_type,
                   currency     = excluded.currency
            """,
            (i.ticker, i.yahoo, i.isin, i.name, i.asset_type, i.currency, i.exchange),
        )
    cx.commit()
    log.info("Catalogue synchronisé : %d instruments", len(instruments))


def get_instrument_id(cx: sqlite3.Connection, inst: Instrument) -> int:
    row = cx.execute(
        "SELECT instrument_id FROM instruments WHERE ticker = ? AND exchange = ?",
        (inst.ticker, inst.exchange),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"{inst.ticker} absent du catalogue en base — lancez d'abord --init"
        )
    return row["instrument_id"]


def last_stored_date(cx: sqlite3.Connection, instrument_id: int) -> date | None:
    row = cx.execute(
        "SELECT MAX(price_date) AS d FROM prices_daily WHERE instrument_id = ?",
        (instrument_id,),
    ).fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


# ---------------------------------------------------------------------
#  Récupération
# ---------------------------------------------------------------------
def download(symbol: str, since: date | None) -> pd.DataFrame:
    """Renvoie l'historique quotidien brut. DataFrame vide si rien à récupérer.

    `auto_adjust=False` est explicite et non négociable : depuis yfinance 0.2.51
    le défaut est True, auquel cas la colonne `Close` est SILENCIEUSEMENT
    remplacée par le cours ajusté et `Adj Close` disparaît. On perdrait la
    possibilité de recouper l'ajustement du fournisseur — ce que le schéma
    cherche précisément à préserver en stockant les deux.
    """
    kwargs = dict(auto_adjust=False, actions=False)
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            tk = yf.Ticker(symbol)
            if since is None:
                df = tk.history(period="max", **kwargs)
            else:
                df = tk.history(start=since.isoformat(), **kwargs)
            return df if df is not None else pd.DataFrame()
        except Exception as exc:                      # noqa: BLE001 — API tierce instable
            last_error = exc
            wait = 2 ** attempt
            log.warning("%s : échec %d/%d (%s) — nouvelle tentative dans %ds",
                        symbol, attempt, MAX_ATTEMPTS, exc, wait)
            time.sleep(wait)

    raise RuntimeError(f"{symbol} : abandon après {MAX_ATTEMPTS} tentatives") from last_error


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Ramène le DataFrame yfinance aux colonnes du schéma, index en date pure."""
    if df.empty:
        return df

    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df = df.copy()
    df.index = pd.DatetimeIndex(idx).normalize()

    # Selon la version et le titre, 'Adj Close' peut manquer : on retombe alors
    # sur Close plutôt que d'insérer NULL en silence.
    if "Adj Close" not in df.columns:
        log.warning("colonne 'Adj Close' absente — repli sur 'Close'")
        df["Adj Close"] = df["Close"]

    expected = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise ValueError(f"colonnes manquantes dans la réponse : {manquantes}")

    df = df[expected].dropna(subset=["Close"])
    return df[~df.index.duplicated(keep="last")]


# ---------------------------------------------------------------------
#  Écriture
# ---------------------------------------------------------------------
def upsert_prices(cx: sqlite3.Connection, instrument_id: int, df: pd.DataFrame) -> int:
    rows = [
        (
            instrument_id,
            stamp.date().isoformat(),
            _to_float(r["Open"]), _to_float(r["High"]), _to_float(r["Low"]),
            _to_float(r["Close"]), _to_float(r["Adj Close"]),
            int(r["Volume"]) if pd.notna(r["Volume"]) else None,
            SOURCE,
        )
        for stamp, r in df.iterrows()
    ]
    if not rows:
        return 0

    cx.executemany(
        """
        INSERT INTO prices_daily
               (instrument_id, price_date, open, high, low, close,
                adjusted_close, volume, source, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (instrument_id, price_date) DO UPDATE SET
               open           = excluded.open,
               high           = excluded.high,
               low            = excluded.low,
               close          = excluded.close,
               adjusted_close = excluded.adjusted_close,
               volume         = excluded.volume,
               source         = excluded.source,
               ingested_at    = excluded.ingested_at
        """,
        rows,
    )
    return len(rows)


def upsert_fx(cx: sqlite3.Connection, pair: str, df: pd.DataFrame) -> int:
    rows = [
        (pair, h.date().isoformat(), _to_float(r["Close"]), SOURCE)
        for h, r in df.iterrows()
        if pd.notna(r["Close"])
    ]
    if not rows:
        return 0
    cx.executemany(
        """
        INSERT INTO fx_rates (currency_pair, rate_date, rate, source, ingested_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT (currency_pair, rate_date) DO UPDATE SET
               rate        = excluded.rate,
               source      = excluded.source,
               ingested_at = excluded.ingested_at
        """,
        rows,
    )
    return len(rows)


def _to_float(v) -> float | None:
    return float(v) if pd.notna(v) else None


# ---------------------------------------------------------------------
#  Orchestration
# ---------------------------------------------------------------------
def process(cx, inst: Instrument, *, full: bool, dry_run: bool) -> dict:
    report = {"ticker": inst.ticker, "yahoo": inst.yahoo, "rows": 0,
             "start": None, "end": None, "status": "ok"}

    instrument_id = get_instrument_id(cx, inst)
    last = None if full else last_stored_date(cx, instrument_id)
    since = last - timedelta(days=OVERLAP_DAYS) if last else None

    if dry_run:
        report["status"] = f"simulé (depuis {since or 'origine'})"
        return report

    df = normalize(download(inst.yahoo, since))

    if df.empty:
        # Ne jamais traiter un retour vide comme un succès : c'est le symptôme
        # d'un symbole mort ou renommé, et l'ignorer laisserait un trou muet.
        report["status"] = "VIDE — symbole à revérifier"
        log.warning("%s (%s) : aucune donnée retournée", inst.ticker, inst.yahoo)
        return report

    report["rows"] = upsert_prices(cx, instrument_id, df)
    report["start"] = df.index.min().date().isoformat()
    report["end"] = df.index.max().date().isoformat()

    # Sur une passe complète, l'historique doit remonter au moins aussi loin que
    # ce qui avait été constaté à la résolution du symbole. S'il s'est nettement
    # raccourci, c'est le signe d'un changement de ligne de cotation chez Yahoo —
    # à traiter, sinon le simulateur proposera une date de départ qu'il ne sait
    # pas honorer. La tolérance absorbe le décalage normal de quelques jours
    # entre la première barre quotidienne et le repère mensuel du catalogue.
    if full:
        gap = (date.fromisoformat(report["start"])
                 - date.fromisoformat(inst.yahoo_since)).days
        if gap > HISTORY_TOLERANCE_DAYS:
            report["status"] = f"historique raccourci de {gap}j (attendu {inst.yahoo_since})"
            log.warning("%s : historique démarre le %s, attendu vers le %s",
                        inst.ticker, report["start"], inst.yahoo_since)

    cx.commit()
    return report


def process_fx(cx, pair: str, symbol: str, *, full: bool, dry_run: bool) -> dict:
    report = {"ticker": pair, "yahoo": symbol, "rows": 0,
             "start": None, "end": None, "status": "ok"}
    if dry_run:
        report["status"] = "simulé"
        return report

    row = cx.execute(
        "SELECT MAX(rate_date) AS d FROM fx_rates WHERE currency_pair = ?", (pair,)
    ).fetchone()
    last = date.fromisoformat(row["d"]) if row and row["d"] else None
    since = None if full or not last else last - timedelta(days=OVERLAP_DAYS)

    df = normalize(download(symbol, since))
    if df.empty:
        report["status"] = "VIDE"
        return report

    report["rows"] = upsert_fx(cx, pair, df)
    report["start"] = df.index.min().date().isoformat()
    report["end"] = df.index.max().date().isoformat()
    cx.commit()
    return report


def print_report(reports: list[dict]) -> None:
    width = max((len(b["ticker"]) for b in reports), default=8)
    print("\n" + "─" * 78)
    print(f"{'TICKER':<{width}}  {'SYMBOLE':<10} {'LIGNES':>7}  {'DÉBUT':<11}{'FIN':<11} STATUT")
    print("─" * 78)
    for b in sorted(reports, key=lambda x: (x["status"] == "ok", x["ticker"])):
        print(f"{b['ticker']:<{width}}  {b['yahoo']:<10} {b['rows']:>7}  "
              f"{b['start'] or '—':<11}{b['end'] or '—':<11} {b['status']}")
    print("─" * 78)

    total = sum(b["rows"] for b in reports)
    failed = [b for b in reports if b["status"] != "ok"]
    print(f"{total} lignes écrites · {len(reports) - len(failed)}/{len(reports)} instruments OK")
    if failed:
        print(f"⚠  À vérifier : {', '.join(b['ticker'] for b in failed)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--init", action="store_true", help="crée le schéma et charge le catalogue")
    p.add_argument("--full", action="store_true", help="rejoue tout l'historique")
    p.add_argument("--tickers", nargs="+", metavar="T", help="limite à ces tickers d'affichage")
    p.add_argument("--fx", action="store_true", help="ingère aussi les paires de change")
    p.add_argument("--dry-run", action="store_true", help="n'écrit rien")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    instruments = all_instruments()
    if args.tickers:
        requested = {t.upper() for t in args.tickers}
        instruments = [i for i in instruments if i.ticker.upper() in requested]
        unknown = requested - {i.ticker.upper() for i in instruments}
        if unknown:
            log.error("Ticker(s) absent(s) du catalogue : %s", ", ".join(sorted(unknown)))
            return 2

    if not instruments:
        log.error("Aucun instrument à traiter.")
        return 2

    cx = open_db(args.db)
    try:
        if args.init:
            init_schema(cx)
        sync_catalogue(cx, all_instruments())

        mode = "complet" if args.full else "incrémental"
        log.info("Ingestion %s — %d instruments -> %s", mode, len(instruments), args.db)

        reports = []
        for n, inst in enumerate(instruments, 1):
            log.info("[%d/%d] %s (%s)", n, len(instruments), inst.ticker, inst.yahoo)
            try:
                reports.append(process(cx, inst, full=args.full, dry_run=args.dry_run))
            except Exception as exc:                  # noqa: BLE001
                log.error("%s : %s", inst.ticker, exc)
                reports.append({"ticker": inst.ticker, "yahoo": inst.yahoo, "rows": 0,
                               "start": None, "end": None, "status": f"ERREUR {exc}"})
            time.sleep(PAUSE_BETWEEN_TICKERS)

        if args.fx:
            for pair, symbol in FX_PAIRS.items():
                log.info("change %s (%s)", pair, symbol)
                try:
                    reports.append(process_fx(cx, pair, symbol,
                                             full=args.full, dry_run=args.dry_run))
                except Exception as exc:              # noqa: BLE001
                    log.error("%s : %s", pair, exc)
                    reports.append({"ticker": pair, "yahoo": symbol, "rows": 0,
                                   "start": None, "end": None, "status": f"ERREUR {exc}"})
                time.sleep(PAUSE_BETWEEN_TICKERS)

        print_report(reports)
        return 1 if any(b["status"] != "ok" for b in reports) else 0
    finally:
        cx.close()


if __name__ == "__main__":
    sys.exit(main())
