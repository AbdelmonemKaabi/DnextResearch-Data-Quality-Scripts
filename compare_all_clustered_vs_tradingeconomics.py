"""Comparaison des totaux mensuels Clustered (deja en USD) avec Trading Economics, pour TOUS les
pays Reporter de Clustered -- version multi-pays de compare_clustered_vs_tradingeconomics.py, qui
ne traite qu'un DataSource (Finlande). Memes regles de comparaison (_compare_country) et meme
mapping pays -> Trading Economics (resolve_te_slug), importes de ce script.

Comparaison par couple (DataSource, pays Reporter) : un meme pays peut apparaitre dans plusieurs
DataSources (ex: JPN dans 3 datasets estat), et les additionner compterait ses echanges plusieurs
fois. Chaque couple est donc compare separement a la serie Trading Economics du pays, scrapee une
seule fois par pays et gardee en cache sur disque (TE_CACHE_DIR) : un run interrompu reprend sans
re-scraper les pays deja faits.

Exclus :
  - les DataSources multi-pays Eurostat et UN Comtrade (EXCLUDED_DATASOURCES) ;
  - les flux autres que Export/Import (RE_IMP, TRANSIT, N-A...) : Trading Economics ne publie que
    les exportations et importations. Ils sont listes par couple dans le resume (other_flows) ;
  - un pays dont la serie Trading Economics n'est pas mensuelle (trimestrielle, annuelle) : la
    comparer mois par mois a Clustered n'aurait pas de sens (statut te_non_mensuel).

Usage :
    python compare_all_clustered_vs_tradingeconomics.py              # tous les pays
    python compare_all_clustered_vs_tradingeconomics.py FIN DEU      # seulement ces codes ISO3
    python compare_all_clustered_vs_tradingeconomics.py --dry-run    # couples + mapping TE, sans scraper
    python compare_all_clustered_vs_tradingeconomics.py --refresh    # ignore le cache TE, re-scrape tout
"""

from __future__ import annotations

import argparse
import os
import time

import pandas as pd

from compare_clustered_vs_tradingeconomics import TOLERANCE, _compare_country, resolve_te_slug, with_percent_text
from exchange_rate import USD_PEGGED_RATES
from results_db import save_run
from snowflake_client import run_query
from te_scraper import SCRAPE_DELAY_SECONDS, scrape_country_trade_series

EXCLUDED_DATASOURCES = [
    "dnexr-eurostat-all-eur-tf-customs-bilateral-monthly",
    "dnexr-un-comtrade-all-all-tf-customs-bilateral-weekly",
]
COMPARED_FLOWS = ("Export", "Import")

# Cache des series Trading Economics scrapees, un CSV par pays (slug TE). --refresh l'ignore.
TE_CACHE_DIR = "te_cache"

# Ecart median maximal (jours) entre deux points consecutifs d'une serie TE pour la considerer
# mensuelle. Au-dela (~91 j trimestriel, ~365 j annuel), le pays est exclu : te_non_mensuel.
MAX_MONTHLY_GAP_DAYS = 45

OUTPUT_FILE = "all_clustered_vs_tradingeconomics_result.csv"
# Tables de la base SQL Server locale (results_db) ou chaque execution ajoute son detail et son resume.
DB_TABLE = "all_clustered_vs_tradingeconomics"
DB_SUMMARY_TABLE = "all_clustered_vs_tradingeconomics_summary"
SUMMARY_FILE = "all_clustered_vs_tradingeconomics_summary.csv"

ALL_CLUSTERED_MONTHLY_QUERY = """
SELECT
    "DataSource",
    COALESCE(NULLIF(TRIM(TO_VARCHAR("Reporter")), ''), 'N/A') AS "Country Reporter",
    DATE_TRUNC('MONTH', "DateReporter"::DATE) AS "Month",
    COALESCE(NULLIF(TRIM(TO_VARCHAR("TradeFlow")), ''), 'N/A') AS "Trade Direction",
    SUM(COALESCE("Value", 0)) AS "Total Value"
FROM DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED"
WHERE "DataSource" NOT IN ({excluded})
GROUP BY 1, 2, 3, 4
"""


def _fetch_all_clustered_totals() -> pd.DataFrame:
    """Totaux mensuels Clustered par (DataSource, Reporter, Month, TradeFlow), hors EXCLUDED_DATASOURCES."""
    placeholders = ", ".join(f"%(excluded_{i})s" for i in range(len(EXCLUDED_DATASOURCES)))
    params = {f"excluded_{i}": ds for i, ds in enumerate(EXCLUDED_DATASOURCES)}
    df = run_query(ALL_CLUSTERED_MONTHLY_QUERY.format(excluded=placeholders), params=params)
    df["Total Value"] = pd.to_numeric(df["Total Value"], errors="raise").astype("float64")
    return df


def _is_te_monthly(te_df: pd.DataFrame) -> bool:
    """True si, pour chaque direction, l'ecart median entre deux mois consecutifs de la serie TE
    est d'au plus MAX_MONTHLY_GAP_DAYS jours (serie mensuelle). Moins de 2 points : False."""
    if te_df.empty:
        return False
    for _, d in te_df.groupby("Direction"):
        months = pd.Series(sorted(pd.to_datetime(d["Month"]).unique()))
        if len(months) < 2 or months.diff().dt.days.median() > MAX_MONTHLY_GAP_DAYS:
            return False
    return True


def _load_or_scrape_te(slug: str, refresh: bool) -> tuple[pd.DataFrame, bool]:
    """Serie TE du pays depuis le cache disque, sinon scrapee (et mise en cache si non vide).
    Renvoie (DataFrame, scrape_effectue)."""
    path = os.path.join(TE_CACHE_DIR, f"{slug}.csv")
    if not refresh and os.path.exists(path):
        te_df = pd.read_csv(path)
        te_df["Month"] = pd.to_datetime(te_df["Month"]).dt.date
        # Conversions manquantes dans le cache pour une devise a parite fixe (ajoutee a
        # USD_PEGGED_RATES apres le scraping) : recalculees ici, sans reseau ni re-scraping.
        pegged = te_df["value_usd"].isna() & te_df["currency"].isin(USD_PEGGED_RATES)
        te_df.loc[pegged, "value_usd"] = te_df.loc[pegged, "value_original"] * te_df.loc[pegged, "currency"].map(USD_PEGGED_RATES)
        return te_df, False

    te_df = scrape_country_trade_series(slug)
    if not te_df.empty:
        os.makedirs(TE_CACHE_DIR, exist_ok=True)
        te_df.to_csv(path, index=False)
    return te_df, True


def _summarize_pair(result: pd.DataFrame) -> dict:
    """Indicateurs d'un couple (DataSource, pays) a partir du resultat de _compare_country.

    signed_gap = (TE - Clustered) / Clustered : positif quand Trading Economics est au-dessus.
    Un ecart median signe stable et loin de zero indique une difference de perimetre entre les
    deux sources plutot que des erreurs ponctuelles (cas de la Finlande : TE toujours ~+3 %).
    """
    both = result.dropna(subset=["value_clustered_usd", "value_te_usd"])
    both = both[both["value_clustered_usd"] != 0]
    signed = (both["value_te_usd"] - both["value_clustered_usd"]) / both["value_clustered_usd"]
    n = len(result)
    n_match = int((result["status"] == "MATCH").sum())
    return {
        "window_start": result["Month"].min() if n else None,
        "window_end": result["Month"].max() if n else None,
        "rows_compared": n,
        "match": n_match,
        "mismatch": int((result["status"] == "MISMATCH").sum()),
        "non_comparable": int((result["status"] == "NON_COMPARABLE").sum()),
        "match_pct": round(n_match / n, 4) if n else None,
        "median_signed_gap": round(float(signed.median()), 4) if len(signed) else None,
        "median_abs_gap": round(float(signed.abs().median()), 4) if len(signed) else None,
        "max_abs_gap": round(float(signed.abs().max()), 4) if len(signed) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Clustered vs Trading Economics, tous pays (hors Eurostat et UN Comtrade).")
    parser.add_argument("countries", nargs="*", help="Codes ISO3 a traiter (defaut : tous).")
    parser.add_argument("--dry-run", action="store_true", help="Liste les couples et le mapping TE, sans scraper.")
    parser.add_argument("--refresh", action="store_true", help="Ignore le cache TE et re-scrape chaque pays.")
    args = parser.parse_args()

    print(f"########## Totaux Clustered (hors {len(EXCLUDED_DATASOURCES)} DataSources exclus) ##########")
    df_all = _fetch_all_clustered_totals()
    pairs = df_all[["DataSource", "Country Reporter"]].drop_duplicates().sort_values(["Country Reporter", "DataSource"])
    if args.countries:
        wanted = {c.strip().upper() for c in args.countries}
        pairs = pairs[pairs["Country Reporter"].isin(wanted)]
    codes = sorted(pairs["Country Reporter"].unique())
    print(f"{len(pairs)} couple(s) (DataSource, pays) pour {len(codes)} pays.")

    slugs = {code: resolve_te_slug(code) for code in codes}
    unmapped = {code: reason for code, (slug, reason) in slugs.items() if slug is None}
    if unmapped:
        print(f"\n[ATTENTION] {len(unmapped)} pays non mappe(s) vers Trading Economics (exclus) :")
        for code, reason in unmapped.items():
            print(f"  - {code} : {reason}")

    if args.dry_run:
        print("\n=== Couples a comparer (dry run) ===")
        for _, p in pairs.iterrows():
            slug = slugs[p["Country Reporter"]][0]
            print(f"  {p['Country Reporter']:6} -> {slug or '(non mappe)':28} {p['DataSource']}")
        return

    all_results, summary_rows = [], []
    mapped_codes = [c for c in codes if slugs[c][0] is not None]
    for i, code in enumerate(mapped_codes, start=1):
        slug = slugs[code][0]
        code_pairs = pairs[pairs["Country Reporter"] == code]["DataSource"].tolist()
        print(f"\n########## [{i}/{len(mapped_codes)}] {code} -> {slug} ({len(code_pairs)} DataSource(s)) ##########")

        te_df, te_error, scraped = None, "", False
        try:
            te_df, scraped = _load_or_scrape_te(slug, args.refresh)
            print("Trading Economics : " + ("scrape" if scraped else "cache") + f" ({len(te_df)} point(s))")
        except Exception as exc:
            te_error = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
            print(f"[ERREUR] Trading Economics {slug} : {te_error}")

        for ds in code_pairs:
            base = {"DataSource": ds, "Country": code, "TE_Slug": slug}
            sub = df_all[(df_all["DataSource"] == ds) & (df_all["Country Reporter"] == code)]
            other_flows = sorted(set(sub["Trade Direction"]) - set(COMPARED_FLOWS))
            base["other_flows"] = ", ".join(other_flows)
            sub = sub[sub["Trade Direction"].isin(COMPARED_FLOWS)]

            if te_error:
                summary_rows.append({**base, "status": "te_echec", "note": te_error})
            elif te_df is None or te_df.empty:
                summary_rows.append({**base, "status": "te_vide", "note": "aucune serie Export/Import lue sur Trading Economics"})
            elif not _is_te_monthly(te_df):
                summary_rows.append({**base, "status": "te_non_mensuel", "note": "serie Trading Economics trimestrielle ou annuelle"})
            elif sub.empty:
                summary_rows.append({**base, "status": "clustered_vide", "note": "aucun flux Export/Import dans Clustered"})
            else:
                print(f"  {ds} :", end=" ")
                result = _compare_country(sub, te_df, tolerance=TOLERANCE)
                if result.empty:
                    summary_rows.append({**base, "status": "aucune_periode_commune", "note": "aucun mois couvert par les deux sources"})
                    continue
                result.insert(0, "TE_Slug", slug)
                result.insert(0, "Country", code)
                result.insert(0, "DataSource", ds)
                all_results.append(result)
                summary_rows.append({**base, "status": "compare", "note": "", **_summarize_pair(result)})

        if scraped and i < len(mapped_codes):
            time.sleep(SCRAPE_DELAY_SECONDS)

    for code, reason in unmapped.items():
        for ds in pairs[pairs["Country Reporter"] == code]["DataSource"]:
            summary_rows.append({"DataSource": ds, "Country": code, "TE_Slug": "", "status": "non_mappe", "note": reason})

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(SUMMARY_FILE, index=False)
    print(f"\nResume par couple exporte dans {SUMMARY_FILE} ({len(summary)} ligne(s))")
    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        with_percent_text(combined).to_csv(OUTPUT_FILE, index=False)
        print(f"Detail mensuel exporte dans {OUTPUT_FILE} ({len(combined)} ligne(s))")

        print("\n=== Resume ===")
        print("Couples par statut :", summary["status"].value_counts().to_dict())
        for status in ("MATCH", "MISMATCH", "NON_COMPARABLE"):
            n = int((combined["status"] == status).sum())
            print(f"  {status:15s}: {n} ({n / len(combined) * 100:.2f}%)")
    else:
        print("\n[ATTENTION] Aucun couple compare.", summary["status"].value_counts().to_dict() if len(summary) else "")

    detail = pd.concat(all_results, ignore_index=True) if all_results else pd.DataFrame()
    save_run({DB_TABLE: detail, DB_SUMMARY_TABLE: summary})


if __name__ == "__main__":
    main()
