"""Diagnostic ponctuel : pourquoi un groupe (Month/Direction/HS Chapter/Quantity Unit) est
NON_COMPARABLE avec reason="conversion_echouee: devise_manquante" alors que la devise existe
manuellement dans Snowflake pour ce chapitre.

Ne modifie rien, ne corrige rien : affiche uniquement 3 diagnostics.

Usage :
    python diagnose_currency_na.py <dataset> [month] [trade_direction] [hs_chapter] [quantity_unit]

Exemple (valeurs par defaut si non precisees) :
    python diagnose_currency_na.py dnexr-dnexr-all-fin-tf-customs-bilateral-monthly \
        2026-01-01 Export 08 KG
"""

from __future__ import annotations

import sys

import pandas as pd

from compare_aggregated_sources import _SAFE_DATASET_NAME_RE
from snowflake_client import run_query

# Reprend exactement les memes expressions d'extraction que SNOWFLAKE_QUERY_TEMPLATE
# (compare_aggregated_sources.py) pour que le diagnostic soit fidele au pipeline reel.
BASE_DATA_CTE = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "Date Reporter"::DATE) AS "Month",
        LEFT(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), 2) AS "HS Chapter",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Trade Direction")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Trade Direction")), ''), 'N/A') AS "Trade Direction",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Quantity Unit")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Quantity Unit")), ''), 'N/A') AS "Quantity Unit",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Quantity 2 Unit")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Quantity 2 Unit")), ''), 'N/A') AS "Quantity 2 Unit",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Currency")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Currency")), ''), 'N/A') AS "Currency",
        "Currency" AS "Currency_raw",
        "Date Reporter", "HS Code",
        "Value",
        COALESCE("Value Unit multiplier", 1) AS "Value Multiplier"
    FROM "{table}"
    WHERE "Date Reporter" >= '2026-01-01' AND "Date Reporter" < '2027-01-01'
      AND NULLIF(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), '') IS NOT NULL
)
"""

DIAG1_BREAKDOWN_BY_CURRENCY = BASE_DATA_CTE + """
SELECT
    "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit", "Currency",
    COUNT(*) AS "Number of Records",
    SUM(COALESCE("Value", 0) * "Value Multiplier") AS "Total Value"
FROM BASE_DATA
WHERE "Month" = %(month)s
  AND "Trade Direction" = %(direction)s
  AND "HS Chapter" = %(hs_chapter)s
  AND "Quantity Unit" = %(quantity_unit)s
GROUP BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit", "Currency"
ORDER BY "Quantity 2 Unit", "Currency"
"""

DIAG3_RAW_CURRENCY_SAMPLE = BASE_DATA_CTE + """
SELECT "Date Reporter", "HS Code", "Currency_raw", "Value"
FROM BASE_DATA
WHERE "Month" = %(month)s
  AND "Trade Direction" = %(direction)s
  AND "HS Chapter" = %(hs_chapter)s
  AND "Quantity Unit" = %(quantity_unit)s
  AND "Currency" = 'N/A'
LIMIT 10
"""


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(
            "Usage: python diagnose_currency_na.py <dataset> [month=2026-01-01] "
            "[trade_direction=Export] [hs_chapter=08] [quantity_unit=KG]"
        )

    dataset = args[0]
    if not _SAFE_DATASET_NAME_RE.match(dataset):
        raise SystemExit(f"Nom de dataset invalide : {dataset!r}")

    month = args[1] if len(args) > 1 else "2026-01-01"
    direction = args[2] if len(args) > 2 else "Export"
    hs_chapter = args[3] if len(args) > 3 else "08"
    quantity_unit = args[4] if len(args) > 4 else "KG"

    params = {
        "month": month,
        "direction": direction,
        "hs_chapter": hs_chapter,
        "quantity_unit": quantity_unit,
    }

    print(f"Dataset={dataset!r} | Month={month} | Direction={direction} | "
          f"HS Chapter={hs_chapter} | Quantity Unit={quantity_unit}\n")

    # --- Diagnostic 1 : repartition brute par Currency (avant fusion finale) ---
    sql1 = DIAG1_BREAKDOWN_BY_CURRENCY.format(table=dataset)
    df1 = run_query(sql1, params=params)
    print("=== Diagnostic 1 : lignes SQL brutes groupees par Currency ===")
    print(df1.to_string(index=False))

    # --- Diagnostic 2 : poids du sous-ensemble "devise manquante" ---
    if not df1.empty:
        total_records = df1["Number of Records"].sum()
        total_value = df1["Total Value"].sum()
        na_mask = df1["Currency"].astype(str).str.strip().str.upper().isin(["", "N/A"])
        na_records = df1.loc[na_mask, "Number of Records"].sum()
        na_value = df1.loc[na_mask, "Total Value"].sum()

        pct_records = (na_records / total_records * 100) if total_records else 0.0
        pct_value = (na_value / total_value * 100) if total_value else 0.0

        print("\n=== Diagnostic 2 : poids du sous-ensemble Currency='N/A' ===")
        print(f"Total groupe        : {total_records} enregistrements, {total_value:,.2f} (valeur brute, devises melangees)")
        print(f"Sous-ensemble N/A    : {na_records} enregistrements ({pct_records:.2f}%), {na_value:,.2f} ({pct_value:.2f}% de la valeur brute)")
        if na_records == 0:
            print("Aucune ligne Currency='N/A' trouvee pour ce groupe precis : "
                  "l'hypothese ne s'applique pas ici, chercher une autre cause (ex. devise "
                  "valide mais non geree par exchange_rate.py -> voir ExchangeRateError).")
    else:
        print("\n(Aucune ligne trouvee pour ce groupe -- verifier les parametres)")

    # --- Diagnostic 3 : contenu reel de la colonne Currency source pour les lignes N/A ---
    sql3 = DIAG3_RAW_CURRENCY_SAMPLE.format(table=dataset)
    df3 = run_query(sql3, params=params)
    print("\n=== Diagnostic 3 : echantillon brut de la colonne Currency source (lignes extraites en N/A) ===")
    if df3.empty:
        print("(vide -- pas de ligne N/A pour ce groupe, coherent avec Diagnostic 2)")
    else:
        with pd.option_context("display.max_colwidth", None):
            print(df3.to_string(index=False))


if __name__ == "__main__":
    main()
