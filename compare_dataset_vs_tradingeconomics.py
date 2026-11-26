"""Comparaison des totaux mensuels d'un dataset source (table Snowflake brute, en devise locale)
convertis en USD avec Trading Economics, sens par sens (Export/Import).

Variante de compare_all_clustered_vs_tradingeconomics.py qui contourne Clustered : la valeur est lue
dans la table du dataset (meme nom que le DataSource Clustered), multipliee par "Value Unit
multiplier", puis convertie en USD mois par mois et devise par devise, avec la meme conversion que
compare_aggregated_sources.py (_convert_snowflake_row_to_usd : parite fixe, Frankfurter, puis
currency-api). Permet de savoir si un ecart Clustered vs Trading Economics vient deja de la source
ou de son chargement dans Clustered.

Le pays du dataset est le Reporter que Clustered associe a ce DataSource : un dataset multi-pays
(plusieurs Reporters, ex: Eurostat) n'a pas de serie Trading Economics unique et est ignore.

Comme pour compare_aggregated_sources.py, seules les lignes avec un code HS numerique sont
gardees, et les codes DEXP/REEX sont ramenes a "Export". Seuls les mois couverts par la serie
Trading Economics sont convertis (evite des centaines d'appels de taux de change inutiles).

Usage :
    python compare_dataset_vs_tradingeconomics.py dnexr-all-che-tf-customs-bilateral-monthly
    python compare_dataset_vs_tradingeconomics.py ds1 ds2
    python compare_dataset_vs_tradingeconomics.py            # datasets de datasets_config.yaml
    python compare_dataset_vs_tradingeconomics.py --refresh  # ignore le cache TE, re-scrape
"""

from __future__ import annotations

import argparse

import pandas as pd
import pycountry

from compare_aggregated_sources import (
    TRADE_DIRECTION_ALIASES,
    _SAFE_DATASET_NAME_RE,
    _convert_snowflake_row_to_usd,
    _resolve_datasets,
)
from compare_all_clustered_vs_tradingeconomics import (
    COMPARED_FLOWS,
    _is_te_monthly,
    _load_or_scrape_te,
    _summarize_pair,
)
from compare_clustered_vs_tradingeconomics import COUNTRY_CODES_QUERY, TOLERANCE, _compare_country, resolve_te_slug
from snowflake_client import run_query

OUTPUT_FILE = "dataset_vs_tradingeconomics_result.csv"
SUMMARY_FILE = "dataset_vs_tradingeconomics_summary.csv"

DATASET_MONTHLY_QUERY_TEMPLATE = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "Date Reporter"::DATE) AS "Month",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Trade Direction")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Trade Direction")), ''), 'N/A') AS "Trade Direction",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Currency")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Currency")), ''), 'N/A') AS "Currency",
        "Value" * COALESCE("Value Unit multiplier", 1) AS "Value"
    FROM "{table}"
    WHERE NULLIF(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), '') IS NOT NULL
)
SELECT "Month", "Trade Direction", "Currency", SUM("Value") AS "Total Value"
FROM BASE_DATA
GROUP BY "Month", "Trade Direction", "Currency"
"""


def _fetch_dataset_totals(dataset: str) -> pd.DataFrame:
    """Totaux mensuels du dataset brut par (Month, Trade Direction, Currency), en devise d'origine."""
    if not _SAFE_DATASET_NAME_RE.match(dataset):
        raise ValueError(f"Nom de dataset invalide (caracteres non autorises) : {dataset!r}")
    return run_query(DATASET_MONTHLY_QUERY_TEMPLATE.format(table=dataset))


def country_from_name(dataset: str) -> list[str]:
    """Codes ISO3 lus dans le nom du dataset (ex: "dnexr-dnexr-all-vnm-tf-..." -> ["VNM"])."""
    tokens = [t.upper() for t in dataset.split("-") if len(t) == 3]
    return sorted({t for t in tokens if pycountry.countries.get(alpha_3=t)})


def _dataset_country(dataset: str) -> list[str]:
    """Codes Reporter que Clustered associe a ce DataSource ; a defaut (dataset absent de
    Clustered, ex: VNM), le code ISO3 lu dans le nom du dataset."""
    df = run_query(COUNTRY_CODES_QUERY, params={"data_source": dataset})
    codes = [c for c in df["Country Code"].dropna().astype(str).str.strip() if c]
    return codes or country_from_name(dataset)


def to_usd_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """Convertit chaque ligne (Month, Trade Direction, Currency) en USD puis somme par
    (Month, Trade Direction), au format attendu par _compare_country ("Total Value" en USD).

    Meme regle que _aggregate_snowflake : si une devise d'un mois ne se convertit pas alors que sa
    valeur est non nulle, le total du mois est NaN (NON_COMPARABLE) plutot que sous-evalue.
    """
    df = df.copy()
    df["Trade Direction"] = df["Trade Direction"].replace(TRADE_DIRECTION_ALIASES)
    df = df[df["Trade Direction"].isin(COMPARED_FLOWS)]
    if df.empty:
        return pd.DataFrame(columns=["Month", "Trade Direction", "Total Value", "conversion_errors"])

    converted = df.apply(_convert_snowflake_row_to_usd, axis=1, result_type="expand")
    df["_usd"] = converted[0].astype("float64")
    df["_error"] = converted[1]

    agg = (
        df.groupby(["Month", "Trade Direction"], as_index=False)
        .agg(
            **{
                "Total Value": ("_usd", "sum"),
                "conversion_errors": ("_error", lambda s: "; ".join(sorted({e for e in s if e}))),
            }
        )
    )
    agg.loc[agg["conversion_errors"] != "", "Total Value"] = float("nan")
    return agg


def compare_dataset(dataset: str, refresh: bool, country: str | None = None) -> tuple[pd.DataFrame | None, dict]:
    """Compare un dataset a Trading Economics. Renvoie (detail mensuel ou None, ligne de resume).
    `country` (ISO3) force le pays au lieu de le deduire de Clustered ou du nom du dataset."""
    base = {"Dataset": dataset}
    codes = [country.strip().upper()] if country else _dataset_country(dataset)
    if len(codes) != 1:
        note = "pays introuvable (Clustered et nom du dataset), utiliser --country" if not codes else f"dataset multi-pays : {codes}"
        return None, {**base, "status": "pays_indetermine", "note": note}

    code = codes[0]
    slug, reason = resolve_te_slug(code)
    base.update({"Country": code, "TE_Slug": slug or ""})
    if slug is None:
        return None, {**base, "status": "non_mappe", "note": reason}

    te_df, _ = _load_or_scrape_te(slug, refresh)
    if te_df.empty:
        return None, {**base, "status": "te_vide", "note": "aucune serie Export/Import lue sur Trading Economics"}
    if not _is_te_monthly(te_df):
        return None, {**base, "status": "te_non_mensuel", "note": "serie Trading Economics trimestrielle ou annuelle"}

    raw = _fetch_dataset_totals(dataset)
    raw["Month"] = pd.to_datetime(raw["Month"]).dt.date
    te_months = pd.to_datetime(te_df["Month"]).dt.date
    raw = raw[(raw["Month"] >= te_months.min()) & (raw["Month"] <= te_months.max())]
    currencies = sorted(raw["Currency"].astype(str).unique())
    print(f"  {code} -> {slug}, devise(s) du dataset : {currencies}")

    monthly = to_usd_monthly(raw)
    if monthly.empty:
        return None, {**base, "status": "dataset_vide", "note": "aucun flux Export/Import sur la periode Trading Economics"}

    result = _compare_country(monthly, te_df, tolerance=TOLERANCE)
    if result.empty:
        return None, {**base, "status": "aucune_periode_commune", "note": "aucun mois couvert par les deux sources"}

    errors = monthly[["Month", "Trade Direction", "conversion_errors"]]
    result = result.merge(errors, on=["Month", "Trade Direction"], how="left")
    result = result.rename(columns={"value_clustered_usd": "value_dataset_usd"})
    result.insert(0, "TE_Slug", slug)
    result.insert(0, "Country", code)
    result.insert(0, "Dataset", dataset)

    # _summarize_pair lit value_clustered_usd : on lui repasse le nom attendu.
    summary = _summarize_pair(result.rename(columns={"value_dataset_usd": "value_clustered_usd"}))
    return result, {**base, "status": "compare", "currencies": ", ".join(currencies), "note": "", **summary}


def main() -> None:
    parser = argparse.ArgumentParser(description="Dataset source (converti en USD) vs Trading Economics.")
    parser.add_argument("datasets", nargs="*", help="Datasets a comparer (defaut : datasets_config.yaml).")
    parser.add_argument("--refresh", action="store_true", help="Ignore le cache TE et re-scrape.")
    parser.add_argument("--country", help="Code ISO3 du pays, si Clustered et le nom du dataset ne le donnent pas.")
    args = parser.parse_args()

    datasets = _resolve_datasets(args.datasets)
    if datasets == "all":
        raise SystemExit("'all' non supporte ici : chaque dataset declenche un scraping Trading Economics, liste-les.")

    all_results, summary_rows = [], []
    for dataset in datasets:
        print(f"\n########## {dataset} ##########")
        try:
            result, summary = compare_dataset(dataset, args.refresh, args.country)
        except Exception as exc:
            error = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
            print(f"[ERREUR] {dataset} : {error}")
            summary_rows.append({"Dataset": dataset, "status": "erreur", "note": error})
            continue
        summary_rows.append(summary)
        if result is not None:
            all_results.append(result)
            counts = result["status"].value_counts().to_dict()
            print(f"  {len(result)} mois/sens compares : {counts}, ecart median signe {summary['median_signed_gap']}")
        else:
            print(f"  {summary['status']} : {summary['note']}")

    pd.DataFrame(summary_rows).to_csv(SUMMARY_FILE, index=False)
    print(f"\nResume exporte dans {SUMMARY_FILE}")
    if all_results:
        pd.concat(all_results, ignore_index=True).to_csv(OUTPUT_FILE, index=False)
        print(f"Detail mensuel exporte dans {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
