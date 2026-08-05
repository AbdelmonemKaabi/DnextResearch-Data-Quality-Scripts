"""Comparaison de totaux agreges (par mois, sens, chapitre HS, unites) entre deux sources Snowflake."""

from __future__ import annotations

import re
import sys

import pandas as pd
import yaml

from exchange_rate import ExchangeRateError, get_monthly_average_rate
from snowflake_client import run_query

DATASETS_CONFIG_FILE = "datasets_config.yaml"

# Nom de dataset valide pour interpolation dans un identifiant SQL (nom de table) : lettres,
# chiffres, tirets, underscores uniquement. Les valeurs bindees (%(...)s) ne peuvent pas etre
# utilisees pour un identifiant, d'ou cette validation manuelle avant interpolation dans le FROM.
_SAFE_DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

GROUP_KEYS = ["Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Unit 2"]

# Conversion vers une unite canonique, par famille, pour rendre les quantites comparables entre
# les deux sources meme quand l'une utilise MT (tonne metrique) et l'autre KG. Une unite absente
# de cette table (volume, nombre de pieces, etc.) reste telle quelle : pas de fusion approximative.
# Si Quantity Unit 2 utilise d'autres familles d'unites (volume...), donner les codes exacts et le
# facteur de conversion pour completer cette table.
WEIGHT_CONVERSION = {
    "KG": 1.0,
    "MT": 1000.0,
}
CANONICAL_WEIGHT_UNIT = "KG"

# ATTENTION : noms de colonnes "Quantity 2" / "Quantity Unit 2" / "Quantity Unit 2 multiplier"
# (Snowflake brut) devines par analogie avec "Quantity" / "Quantity Unit" / "Quantity Unit
# multiplier" -- a confirmer/corriger si Snowflake renvoie une erreur "colonne introuvable".
CLUSTERED_QUERY = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "DateReporter"::DATE) AS "Month",
        LEFT(LPAD(REGEXP_REPLACE(TO_VARCHAR("HSCode"), '[^0-9]', ''), 8, '0'), 2) AS "HS Chapter",
        COALESCE(NULLIF(TRIM(UPPER(TO_VARCHAR("Unit"))), ''), 'N/A') AS "Quantity Unit",
        COALESCE(NULLIF(TRIM(UPPER(TO_VARCHAR("Unit2"))), ''), 'N/A') AS "Quantity Unit 2",
        COALESCE(NULLIF(TRIM(TO_VARCHAR("TradeDirection")), ''), 'N/A') AS "Trade Direction",
        COALESCE(NULLIF(TRIM(TO_VARCHAR("Currency")), ''), 'N/A') AS "Currency",
        "Quantity",
        "Quantity2",
        "Value"
    FROM DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED"
    WHERE "DataSource" = %(data_source)s
      AND "YearMonth" LIKE '2026%%'
      AND NULLIF(REGEXP_REPLACE(TO_VARCHAR("HSCode"), '[^0-9]', ''), '') IS NOT NULL
)
SELECT
    "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Unit 2", "Currency",
    COUNT(*) AS "Number of Records",
    SUM(COALESCE("Quantity", 0)) AS "Total Quantity",
    SUM(COALESCE("Quantity2", 0)) AS "Total Quantity 2",
    SUM(COALESCE("Value", 0)) AS "Total Value"
FROM BASE_DATA
GROUP BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Unit 2", "Currency"
ORDER BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Unit 2", "Currency"
"""

SNOWFLAKE_QUERY_TEMPLATE = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "Date Reporter"::DATE) AS "Month",
        LEFT(LPAD(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), 8, '0'), 2) AS "HS Chapter",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Trade Direction")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Trade Direction")), ''), 'N/A') AS "Trade Direction",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Quantity Unit")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Quantity Unit")), ''), 'N/A') AS "Quantity Unit",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Quantity Unit 2")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Quantity Unit 2")), ''), 'N/A') AS "Quantity Unit 2",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Currency")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Currency")), ''), 'N/A') AS "Currency",
        "Quantity", "Quantity 2", "Value",
        COALESCE("Quantity Unit multiplier", 1) AS "Quantity Multiplier",
        COALESCE("Quantity Unit 2 multiplier", 1) AS "Quantity Multiplier 2",
        COALESCE("Value Unit multiplier", 1) AS "Value Multiplier"
    FROM "{table}"
    WHERE "Date Reporter" >= '2026-01-01' AND "Date Reporter" < '2027-01-01'
      AND NULLIF(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), '') IS NOT NULL
)
SELECT
    "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Unit 2",
    "Quantity Multiplier", "Quantity Multiplier 2", "Currency", "Value Multiplier",
    COUNT(*) AS "Number of Records",
    SUM(COALESCE("Quantity", 0) * "Quantity Multiplier") AS "Total Quantity",
    SUM(COALESCE("Quantity 2", 0) * "Quantity Multiplier 2") AS "Total Quantity 2",
    SUM(COALESCE("Value", 0) * "Value Multiplier") AS "Total Value"
FROM BASE_DATA
GROUP BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Unit 2",
    "Quantity Multiplier", "Quantity Multiplier 2", "Currency", "Value Multiplier"
ORDER BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity Unit 2",
    "Quantity Multiplier", "Quantity Multiplier 2", "Currency", "Value Multiplier"
"""


def _build_snowflake_query(dataset: str) -> str:
    """Insere `dataset` comme nom de table dans SNOWFLAKE_QUERY_TEMPLATE (FROM "{table}").

    Un nom de table ne peut pas etre passe en parametre lie (%(...)s) : seule une valeur peut
    l'etre, pas un identifiant. `dataset` est donc valide contre _SAFE_DATASET_NAME_RE avant
    interpolation directe, pour eviter toute injection SQL via ce nom.
    """
    if not _SAFE_DATASET_NAME_RE.match(dataset):
        raise ValueError(f"Nom de dataset invalide (caracteres non autorises) : {dataset!r}")
    return SNOWFLAKE_QUERY_TEMPLATE.format(table=dataset)


def _load_dataset_config(path: str = DATASETS_CONFIG_FILE):
    """Charge la cle "datasets" du fichier de config YAML. None si le fichier n'existe pas."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return None
    return config.get("datasets")


def _resolve_datasets(cli_args: list[str]) -> list[str] | str:
    """Determine la liste de datasets a traiter (ou "all").

    Priorite : arguments en ligne de commande (python compare_aggregated_sources.py <ds1> <ds2>
    ou "all"), sinon la cle "datasets" de DATASETS_CONFIG_FILE.
    """
    raw = cli_args if cli_args else _load_dataset_config()

    if raw is None:
        raise SystemExit(
            "Aucun dataset specifie. Passe-le en argument "
            "(python compare_aggregated_sources.py <dataset> [<dataset2> ...] ou 'all'), "
            f"ou renseigne la cle 'datasets' dans {DATASETS_CONFIG_FILE}."
        )

    if isinstance(raw, str):
        raw = [raw]

    if len(raw) == 1 and raw[0].strip().lower() == "all":
        return "all"

    return list(raw)


def _list_all_datasets() -> list[str]:
    """Renvoie tous les DataSource distincts presents dans la table Clustered."""
    df = run_query(
        'SELECT DISTINCT "DataSource" FROM DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED" '
        'ORDER BY "DataSource"'
    )
    return df["DataSource"].dropna().astype(str).tolist()


def _normalize_unit_column(
    df: pd.DataFrame, quantity_col: str, unit_col: str, conversion_table: dict, canonical_unit: str
) -> pd.DataFrame:
    """Convertit `quantity_col` vers `canonical_unit` et relabellise `unit_col` en consequence.

    Seules les unites presentes dans `conversion_table` sont converties/fusionnees. Une unite
    inconnue (autre famille : volume, nombre de pieces...) reste dans son propre groupe.
    """
    df = df.copy()
    unit_clean = df[unit_col].astype(str).str.strip().str.upper()
    multiplier = unit_clean.map(conversion_table)
    known = multiplier.notna()

    df.loc[known, quantity_col] = df.loc[known, quantity_col] * multiplier[known]
    df[unit_col] = unit_clean.where(~known, canonical_unit)
    return df


def _normalize_quantities(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise les deux couples quantite/unite (Quantity Unit et Quantity Unit 2)."""
    df = _normalize_unit_column(df, "Total Quantity", "Quantity Unit", WEIGHT_CONVERSION, CANONICAL_WEIGHT_UNIT)
    df = _normalize_unit_column(df, "Total Quantity 2", "Quantity Unit 2", WEIGHT_CONVERSION, CANONICAL_WEIGHT_UNIT)
    return df


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
    """Normalise les unites, convertit chaque ligne en USD, puis somme par GROUP_KEYS.

    Si au moins une devise du groupe echoue a se convertir, total_value_snowflake_usd est mis a
    NaN et value_reason renseigne (le groupe entier est NON_COMPARABLE cote valeur), plutot que
    de sommer partiellement les devises qui ont reussi (evite un total sous-evalue silencieux).
    Les quantites et Number of Records restent independants de la conversion de devise.
    """
    df = _normalize_quantities(df)

    converted = df.apply(_convert_snowflake_row_to_usd, axis=1, result_type="expand")
    df["_value_usd"] = converted[0]
    df["_convert_reason"] = converted[1]

    agg = (
        df.groupby(GROUP_KEYS, dropna=False)
        .agg(
            number_of_records_snowflake=("Number of Records", "sum"),
            total_quantity_snowflake=("Total Quantity", "sum"),
            total_quantity2_snowflake=("Total Quantity 2", "sum"),
            total_value_snowflake_usd=("_value_usd", "sum"),
            _failed_reasons=("_convert_reason", lambda s: "; ".join(sorted({r for r in s if r}))),
        )
        .reset_index()
    )

    has_failure = agg["_failed_reasons"] != ""
    agg.loc[has_failure, "total_value_snowflake_usd"] = float("nan")
    agg["value_reason"] = ""
    agg.loc[has_failure, "value_reason"] = "conversion_echouee: " + agg.loc[has_failure, "_failed_reasons"]

    return agg.drop(columns=["_failed_reasons"])


def _aggregate_clustered(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise les unites puis somme par GROUP_KEYS (Clustered est toujours en USD, aucune conversion)."""
    df = _normalize_quantities(df)
    return (
        df.groupby(GROUP_KEYS, dropna=False)
        .agg(
            number_of_records_clustered=("Number of Records", "sum"),
            total_quantity_clustered=("Total Quantity", "sum"),
            total_quantity2_clustered=("Total Quantity 2", "sum"),
            total_value_clustered_usd=("Total Value", "sum"),
        )
        .reset_index()
    )


def _compare_metric(
    matched: pd.DataFrame, snowflake_col: str, clustered_col: str, metric_name: str, tolerance: float
) -> tuple[list, list, list]:
    """Calcule pct_diff/status/reason pour une paire de colonnes (snowflake_col vs clustered_col)."""
    pct_diffs, statuses, reasons = [], [], []
    for _, row in matched.iterrows():
        clustered_val = row[clustered_col]
        if clustered_val == 0:
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append(f"clustered_{metric_name}_zero")
        else:
            pct_diff = abs(row[snowflake_col] - clustered_val) / abs(clustered_val)
            pct_diffs.append(pct_diff)
            statuses.append("MATCH" if pct_diff <= tolerance else "MISMATCH")
            reasons.append("")
    return pct_diffs, statuses, reasons


def compare_aggregated(df_snowflake: pd.DataFrame, df_clustered: pd.DataFrame, tolerance: float = 0.03) -> pd.DataFrame:
    """Agrege, convertit en USD, joint (inner) et compare les deux sources par GROUP_KEYS.

    Trois comparaisons independantes par groupe : valeur (total_value_*_usd), quantite
    (total_quantity_*) et quantite 2 (total_quantity2_*), chacune avec son propre
    pct_diff/status/reason.
    """
    agg_snowflake = _aggregate_snowflake(df_snowflake)
    agg_clustered = _aggregate_clustered(df_clustered)

    total_groups = len(agg_snowflake)

    merged = pd.merge(agg_snowflake, agg_clustered, on=GROUP_KEYS, how="left", indicator=True)
    unmatched_count = int((merged["_merge"] == "left_only").sum())
    matched = merged[merged["_merge"] == "both"].drop(columns=["_merge"]).copy()

    # Comparaison valeur : cas particulier, echec de conversion devise prioritaire sur clustered==0.
    value_pct_diffs, value_statuses, value_reasons = [], [], []
    for _, row in matched.iterrows():
        if row["value_reason"]:
            value_pct_diffs.append(None)
            value_statuses.append("NON_COMPARABLE")
            value_reasons.append(row["value_reason"])
        elif row["total_value_clustered_usd"] == 0:
            value_pct_diffs.append(None)
            value_statuses.append("NON_COMPARABLE")
            value_reasons.append("clustered_value_zero")
        else:
            pct_diff = abs(row["total_value_snowflake_usd"] - row["total_value_clustered_usd"]) / abs(row["total_value_clustered_usd"])
            value_pct_diffs.append(pct_diff)
            value_statuses.append("MATCH" if pct_diff <= tolerance else "MISMATCH")
            value_reasons.append("")

    qty_pct_diffs, qty_statuses, qty_reasons = _compare_metric(
        matched, "total_quantity_snowflake", "total_quantity_clustered", "quantity", tolerance
    )
    qty2_pct_diffs, qty2_statuses, qty2_reasons = _compare_metric(
        matched, "total_quantity2_snowflake", "total_quantity2_clustered", "quantity2", tolerance
    )

    output = pd.DataFrame(
        {
            "Month": matched["Month"],
            "Trade Direction": matched["Trade Direction"],
            "HS Chapter": matched["HS Chapter"],
            "Quantity Unit": matched["Quantity Unit"],
            "Quantity Unit 2": matched["Quantity Unit 2"],
            "number_of_records_snowflake": matched["number_of_records_snowflake"],
            "number_of_records_clustered": matched["number_of_records_clustered"],
            "total_quantity_snowflake": matched["total_quantity_snowflake"],
            "total_quantity_clustered": matched["total_quantity_clustered"],
            "pct_diff_quantity": qty_pct_diffs,
            "status_quantity": qty_statuses,
            "reason_quantity": qty_reasons,
            "total_quantity2_snowflake": matched["total_quantity2_snowflake"],
            "total_quantity2_clustered": matched["total_quantity2_clustered"],
            "pct_diff_quantity2": qty2_pct_diffs,
            "status_quantity2": qty2_statuses,
            "reason_quantity2": qty2_reasons,
            "total_value_snowflake_usd": matched["total_value_snowflake_usd"],
            "total_value_clustered_usd": matched["total_value_clustered_usd"],
            "pct_diff_value": value_pct_diffs,
            "status_value": value_statuses,
            "reason_value": value_reasons,
        }
    ).reset_index(drop=True)

    def _summary(status_col: str, label: str) -> None:
        n_match = int((output[status_col] == "MATCH").sum())
        n_mismatch = int((output[status_col] == "MISMATCH").sum())
        n_non_comparable = int((output[status_col] == "NON_COMPARABLE").sum()) + unmatched_count
        pct_mismatch = (n_mismatch / total_groups * 100) if total_groups else 0.0
        print(f"\n=== Resume {label} ===")
        print(f"Groupes compares (total Snowflake) : {total_groups}")
        print(f"  MATCH           : {n_match}")
        print(f"  MISMATCH        : {n_mismatch}")
        print(f"  NON_COMPARABLE  : {n_non_comparable} (dont {unmatched_count} sans correspondance trouvee)")
        print(f"  % MISMATCH      : {pct_mismatch:.2f}%")

    _summary("status_value", "comparaison Value")
    _summary("status_quantity", "comparaison Quantity")
    _summary("status_quantity2", "comparaison Quantity 2")

    return output


if __name__ == "__main__":
    datasets = _resolve_datasets(sys.argv[1:])

    if datasets == "all":
        datasets = _list_all_datasets()
        print(f"Mode 'all' : {len(datasets)} dataset(s) trouve(s) dans Clustered : {datasets}")

    all_results = []
    failed_datasets = []

    for dataset in datasets:
        print(f"\n########## Dataset : {dataset} ##########")
        try:
            df_clustered = run_query(CLUSTERED_QUERY, params={"data_source": dataset})
            df_snowflake = run_query(_build_snowflake_query(dataset))
            result = compare_aggregated(df_snowflake, df_clustered, tolerance=0.03)
            result.insert(0, "Dataset", dataset)
            all_results.append(result)
        except Exception as exc:
            print(f"[ERREUR] Dataset '{dataset}' ignore : {exc}")
            failed_datasets.append(dataset)

    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        combined.to_csv("aggregated_comparison_result.csv", index=False)
        print(
            f"\nResultat exporte dans aggregated_comparison_result.csv "
            f"({len(combined)} lignes, {len(all_results)} dataset(s))"
        )

    if failed_datasets:
        print(f"\nDatasets en echec (ignores) : {failed_datasets}")
