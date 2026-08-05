"""Comparaison de totaux agreges (par pays, chapitre HS, mois...) entre deux sources Snowflake."""

from __future__ import annotations

import pandas as pd

from exchange_rate import ExchangeRateError, get_monthly_average_rate
from snowflake_client import run_query

GROUP_KEYS = ["Month", "Country Reporter", "Trade Direction", "HS Chapter", "Quantity Unit"]

CLUSTERED_QUERY = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "DateReporter"::DATE) AS "Month",
        LEFT(LPAD(REGEXP_REPLACE(TO_VARCHAR("HSCode"), '[^0-9]', ''), 8, '0'), 2) AS "HS Chapter",
        COALESCE(NULLIF(TRIM(UPPER(TO_VARCHAR("Unit"))), ''), 'N/A') AS "Quantity Unit",
        COALESCE(NULLIF(TRIM(TO_VARCHAR("TradeDirection")), ''), 'N/A') AS "Trade Direction",
        COALESCE(NULLIF(TRIM(TO_VARCHAR("Currency")), ''), 'N/A') AS "Currency",
        COALESCE(NULLIF(TRIM(TO_VARCHAR("Reporter")), ''), 'N/A') AS "Country Reporter",
        "Quantity",
        "Value"
    FROM DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED"
    WHERE "DataSource" = 'dnexr-dnexr-all-fin-tf-customs-bilateral-monthly'
      AND "YearMonth" LIKE '2026%'
      AND NULLIF(REGEXP_REPLACE(TO_VARCHAR("HSCode"), '[^0-9]', ''), '') IS NOT NULL
)
SELECT
    "Month", "Country Reporter", "Trade Direction", "HS Chapter", "Quantity Unit", "Currency",
    COUNT(*) AS "Number of Records",
    SUM(COALESCE("Quantity", 0)) AS "Total Quantity",
    SUM(COALESCE("Value", 0)) AS "Total Value"
FROM BASE_DATA
GROUP BY "Month", "Country Reporter", "Trade Direction", "HS Chapter", "Quantity Unit", "Currency"
ORDER BY "Month", "Country Reporter", "Trade Direction", "HS Chapter", "Quantity Unit", "Currency"
"""

SNOWFLAKE_QUERY = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "Date Reporter"::DATE) AS "Month",
        LEFT(LPAD(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), 8, '0'), 2) AS "HS Chapter",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Country Reporter")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Country Reporter")), ''), 'N/A') AS "Country Reporter",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Trade Direction")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Trade Direction")), ''), 'N/A') AS "Trade Direction",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Quantity Unit")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Quantity Unit")), ''), 'N/A') AS "Quantity Unit",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Currency")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Currency")), ''), 'N/A') AS "Currency",
        "Quantity", "Value",
        COALESCE("Quantity Unit multiplier", 1) AS "Quantity Multiplier",
        COALESCE("Value Unit multiplier", 1) AS "Value Multiplier"
    FROM "dnexr-dnexr-all-fin-tf-customs-bilateral-monthly"
    WHERE "Date Reporter" >= '2026-01-01' AND "Date Reporter" < '2027-01-01'
      AND NULLIF(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), '') IS NOT NULL
)
SELECT
    "Month", "Country Reporter", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Multiplier", "Currency", "Value Multiplier",
    COUNT(*) AS "Number of Records",
    SUM(COALESCE("Quantity", 0) * "Quantity Multiplier") AS "Total Quantity",
    SUM(COALESCE("Value", 0) * "Value Multiplier") AS "Total Value"
FROM BASE_DATA
GROUP BY "Month", "Country Reporter", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Multiplier", "Currency", "Value Multiplier"
ORDER BY "Month", "Country Reporter", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Multiplier", "Currency", "Value Multiplier"
"""


def _convert_snowflake_row_to_usd(row) -> tuple[float | None, str]:
    """Convertit "Total Value" en USD pour une ligne agregee Snowflake (deja multiplie cote SQL).

    Retourne (valeur_usd_ou_None, reason). reason == "" si la conversion a reussi.
    """
    value = row["Total Value"]
    if pd.isna(value):
        return None, "valeur_manquante"

    currency = row["Currency"]
    currency_clean = str(currency).strip().upper() if pd.notna(currency) else ""
    if not currency_clean or currency_clean == "N/A":
        return None, "devise_manquante"

    if currency_clean == "USD":
        return float(value), ""

    month = row["Month"]
    try:
        rate_info = get_monthly_average_rate(currency_clean, month.year, month.month)
    except ExchangeRateError as exc:
        return None, f"{currency_clean}: {exc}"

    return float(value) * rate_info["rate"], ""


def _aggregate_snowflake(df: pd.DataFrame) -> pd.DataFrame:
    """Convertit chaque ligne en USD puis somme par GROUP_KEYS.

    Si au moins une devise du groupe echoue a se convertir, tout le groupe est marque
    (total_snowflake_usd = NaN, snowflake_reason = "conversion_echouee: ...") plutot que
    de sommer partiellement les devises qui ont reussi (evite un total sous-evalue silencieux).
    """
    df = df.copy()
    converted = df.apply(_convert_snowflake_row_to_usd, axis=1, result_type="expand")
    df["_value_usd"] = converted[0]
    df["_convert_reason"] = converted[1]

    agg = (
        df.groupby(GROUP_KEYS, dropna=False)
        .agg(
            total_snowflake_usd=("_value_usd", "sum"),
            _failed_reasons=("_convert_reason", lambda s: "; ".join(sorted({r for r in s if r}))),
        )
        .reset_index()
    )

    has_failure = agg["_failed_reasons"] != ""
    agg.loc[has_failure, "total_snowflake_usd"] = float("nan")
    agg["snowflake_reason"] = ""
    agg.loc[has_failure, "snowflake_reason"] = "conversion_echouee: " + agg.loc[has_failure, "_failed_reasons"]

    return agg.drop(columns=["_failed_reasons"])


def _aggregate_clustered(df: pd.DataFrame) -> pd.DataFrame:
    """Somme "Total Value" par GROUP_KEYS (Clustered est toujours en USD, aucune conversion)."""
    return (
        df.groupby(GROUP_KEYS, dropna=False)["Total Value"]
        .sum()
        .reset_index()
        .rename(columns={"Total Value": "total_clustered_usd"})
    )


def compare_aggregated(df_snowflake: pd.DataFrame, df_clustered: pd.DataFrame, tolerance: float = 0.03) -> pd.DataFrame:
    """Agrege, convertit en USD, joint (inner) et compare les deux sources par GROUP_KEYS."""
    agg_snowflake = _aggregate_snowflake(df_snowflake)
    agg_clustered = _aggregate_clustered(df_clustered)

    total_groups = len(agg_snowflake)

    merged = pd.merge(agg_snowflake, agg_clustered, on=GROUP_KEYS, how="left", indicator=True)
    unmatched_count = int((merged["_merge"] == "left_only").sum())
    matched = merged[merged["_merge"] == "both"].drop(columns=["_merge"]).copy()

    pct_diffs, statuses, reasons = [], [], []
    for _, row in matched.iterrows():
        if row["snowflake_reason"]:
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append(row["snowflake_reason"])
        elif row["total_clustered_usd"] == 0:
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append("clustered_value_zero")
        else:
            pct_diff = abs(row["total_snowflake_usd"] - row["total_clustered_usd"]) / abs(row["total_clustered_usd"])
            pct_diffs.append(pct_diff)
            statuses.append("MATCH" if pct_diff <= tolerance else "MISMATCH")
            reasons.append("")

    output = pd.DataFrame(
        {
            "Month": matched["Month"],
            "Country Reporter": matched["Country Reporter"],
            "Trade Direction": matched["Trade Direction"],
            "HS Chapter": matched["HS Chapter"],
            "Quantity Unit": matched["Quantity Unit"],
            "total_snowflake_usd": matched["total_snowflake_usd"],
            "total_clustered_usd": matched["total_clustered_usd"],
            "pct_diff": pct_diffs,
            "status": statuses,
            "reason": reasons,
        }
    ).reset_index(drop=True)

    n_match = int((output["status"] == "MATCH").sum())
    n_mismatch = int((output["status"] == "MISMATCH").sum())
    n_non_comparable = int((output["status"] == "NON_COMPARABLE").sum()) + unmatched_count
    pct_mismatch = (n_mismatch / total_groups * 100) if total_groups else 0.0

    print("\n=== Resume de la comparaison agregee ===")
    print(f"Groupes compares (total Snowflake) : {total_groups}")
    print(f"  MATCH           : {n_match}")
    print(f"  MISMATCH        : {n_mismatch}")
    print(f"  NON_COMPARABLE  : {n_non_comparable} (dont {unmatched_count} sans correspondance trouvee)")
    print(f"  % MISMATCH      : {pct_mismatch:.2f}%")

    return output


if __name__ == "__main__":
    df_clustered = run_query(CLUSTERED_QUERY)
    df_snowflake = run_query(SNOWFLAKE_QUERY)
    result = compare_aggregated(df_snowflake, df_clustered, tolerance=0.03)
    result.to_csv("aggregated_comparison_result.csv", index=False)
    print("\nResultat exporte dans aggregated_comparison_result.csv")
