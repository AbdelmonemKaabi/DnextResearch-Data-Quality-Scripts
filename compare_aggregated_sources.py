"""Comparaison de totaux agreges (par mois, sens, chapitre HS, unites) entre deux sources Snowflake."""

from __future__ import annotations

import re
import sys

import pandas as pd
import yaml

from exchange_rate import ExchangeRateError, get_monthly_average_rate, get_mop_to_usd_rate
from snowflake_client import run_query

DATASETS_CONFIG_FILE = "datasets_config.yaml"

# Nom de dataset valide pour interpolation dans un identifiant SQL (nom de table) : lettres,
# chiffres, tirets, underscores uniquement. Les valeurs bindees (%(...)s) ne peuvent pas etre
# utilisees pour un identifiant, d'ou cette validation manuelle avant interpolation dans le FROM.
_SAFE_DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

GROUP_KEYS = ["Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit"]

# Conversion vers une unite canonique, par famille physique, pour rendre les quantites
# comparables entre les deux sources meme quand l'une utilise MT (tonne metrique) et l'autre KG.
# Une table par famille (poids/volume/longueur/surface) : seules des unites de la meme grandeur
# physique peuvent fusionner entre elles, jamais deux grandeurs differentes (ex: KG et M2).
WEIGHT_CONVERSION = {
    "KG": 1.0,
    "MT": 1000.0,
    "LB": 0.45359237,  # 1 LB = 0.45359237 KG
    "GRAM": 0.001,  # 1 GRAM = 0.001 KG
}
CANONICAL_WEIGHT_UNIT = "KG"

VOLUME_CONVERSION = {
    "L": 1.0,
    "ML": 0.001,  # 1 ML = 0.001 L
    "M3": 1000.0,  # 1 M3 = 1000 L
    "KILO_LITER": 1000.0,  # 1 KILO_LITER = 1000 L
    "CM3": 0.001,  # 1 CM3 = 0.001 L
    "CUFT": 28.316846592,  # 1 cuft (pied cube) = 28.316846592 L -- cle en MAJUSCULE : le
    # code est normalise via .str.upper() avant lookup (_normalize_unit_column), donc "cuft"
    # brut devient "CUFT" au moment du match.
}
CANONICAL_VOLUME_UNIT = "L"

LENGTH_CONVERSION = {
    "M": 1.0,
    "INCH": 0.0254,  # 1 INCH = 0.0254 M
    "KILOMTR": 1000.0,  # 1 KILOMTR = 1000 M
}
CANONICAL_LENGTH_UNIT = "M"

AREA_CONVERSION = {
    "M2": 1.0,
}
CANONICAL_AREA_UNIT = "M2"

# Codes rencontres dans les donnees reelles mais volontairement NON ajoutes ci-dessus : sens ou
# facteur de conversion incertain, ne pas les ajouter sans confirmation prealable.
#   - GROSS_TONNAGE : malgre le nom, ce n'est PAS un poids de cargaison mais une capacite de
#     navire -- ne jamais fusionner avec KG/MT.
#   - KILOGRAM_90_DRY, DMT : poids ajustes a l'humidite, pas equivalents a un poids brut.
#   - TONNE : probablement un alias de MT, a confirmer avant fusion.
#   - SHTN, LTN, OZT : tonnes/once non-metriques, facteurs differents de MT/KG.
#   - MTK, CMK : possiblement des unites de surface, sens incertain.
#   - BBL, FLOZ, MLT, HLIT : volumes avec plusieurs variantes possibles selon le pays/contexte.

# Familles appliquees dans cet ordre. Les tables sont disjointes (un code d'unite n'apparait que
# dans une seule table), donc l'ordre d'application n'a pas d'impact sur le resultat. Une unite
# absente de toutes ces tables (UNIT, PIECE, PAIR...) reste dans son propre groupe : ce sont des
# grandeurs non fongibles entre elles (comptage de pieces, pas une mesure physique continue), donc
# aucune conversion n'est inventee pour elles. Pour completer une famille ou en ajouter une,
# donner les codes exacts et le facteur de conversion vers l'unite canonique de la famille.
UNIT_CONVERSION_FAMILIES = [
    (WEIGHT_CONVERSION, CANONICAL_WEIGHT_UNIT),
    (VOLUME_CONVERSION, CANONICAL_VOLUME_UNIT),
    (LENGTH_CONVERSION, CANONICAL_LENGTH_UNIT),
    (AREA_CONVERSION, CANONICAL_AREA_UNIT),
]

# Unites de comptage attendues : jamais convertibles entre elles (grandeurs non fongibles), donc
# absentes de UNIT_CONVERSION_FAMILIES, mais ne doivent pas non plus remonter comme "unite
# inconnue" dans _find_unrecognized_units.
EXPECTED_COUNTING_UNITS = {"UNIT", "PIECE", "PAIR"}

KNOWN_FAMILY_UNITS = {unit for table, _ in UNIT_CONVERSION_FAMILIES for unit in table}


def _find_unrecognized_units(df: pd.DataFrame, unit_col: str) -> set[str]:
    """Renvoie les codes d'unite (normalises comme le fait _normalize_unit_column, trim+upper)
    presents dans `unit_col` qui ne sont ni dans une famille de conversion connue, ni dans les
    unites de comptage attendues -- a passer en revue manuellement avant qu'ils ne causent un
    mismatch silencieux (fusion manquante ou groupe isole a tort).
    """
    cleaned = df[unit_col].astype(str).str.strip().str.upper()
    return set(cleaned.unique()) - KNOWN_FAMILY_UNITS - EXPECTED_COUNTING_UNITS - {"N/A", "NAN"}

# ATTENTION : noms de colonnes "Quantity 2" / "Quantity 2 Unit" / "Quantity 2 Unit multiplier"
# (Snowflake brut) devines par analogie avec "Quantity" / "Quantity Unit" / "Quantity Unit
# multiplier" -- a confirmer/corriger si Snowflake renvoie une erreur "colonne introuvable".
CLUSTERED_QUERY = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "DateReporter"::DATE) AS "Month",
        LEFT(LPAD(REGEXP_REPLACE(TO_VARCHAR("HSCode"), '[^0-9]', ''), 8, '0'), 2) AS "HS Chapter",
        COALESCE(NULLIF(TRIM(UPPER(TO_VARCHAR("Unit"))), ''), 'N/A') AS "Quantity Unit",
        COALESCE(NULLIF(TRIM(UPPER(TO_VARCHAR("Unit2"))), ''), 'N/A') AS "Quantity 2 Unit",
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
    "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit", "Currency",
    COUNT(*) AS "Number of Records",
    SUM(COALESCE("Quantity", 0)) AS "Total Quantity",
    SUM(COALESCE("Quantity2", 0)) AS "Total Quantity 2",
    SUM(COALESCE("Value", 0)) AS "Total Value"
FROM BASE_DATA
GROUP BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit", "Currency"
ORDER BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit", "Currency"
"""

SNOWFLAKE_QUERY_TEMPLATE = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "Date Reporter"::DATE) AS "Month",
        LEFT(LPAD(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), 8, '0'), 2) AS "HS Chapter",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Trade Direction")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Trade Direction")), ''), 'N/A') AS "Trade Direction",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Quantity Unit")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Quantity Unit")), ''), 'N/A') AS "Quantity Unit",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Quantity 2 Unit")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Quantity 2 Unit")), ''), 'N/A') AS "Quantity 2 Unit",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Currency")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Currency")), ''), 'N/A') AS "Currency",
        "Quantity", "Quantity 2", "Value",
        COALESCE("Quantity Unit multiplier", 1) AS "Quantity Multiplier",
        COALESCE("Quantity 2 Unit multiplier", 1) AS "Quantity Multiplier 2",
        COALESCE("Value Unit multiplier", 1) AS "Value Multiplier"
    FROM "{table}"
    WHERE "Date Reporter" >= '2026-01-01' AND "Date Reporter" < '2027-01-01'
      AND NULLIF(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), '') IS NOT NULL
)
SELECT
    "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit",
    "Quantity Multiplier", "Quantity Multiplier 2", "Currency", "Value Multiplier",
    COUNT(*) AS "Number of Records",
    SUM(COALESCE("Quantity", 0) * "Quantity Multiplier") AS "Total Quantity",
    SUM(COALESCE("Quantity 2", 0) * "Quantity Multiplier 2") AS "Total Quantity 2",
    SUM(COALESCE("Value", 0) * "Value Multiplier") AS "Total Value"
FROM BASE_DATA
GROUP BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit",
    "Quantity Multiplier", "Quantity Multiplier 2", "Currency", "Value Multiplier"
ORDER BY "Month", "Trade Direction", "HS Chapter", "Quantity Unit", "Quantity 2 Unit",
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
    # Le connecteur Snowflake renvoie souvent les colonnes NUMBER sous forme de
    # decimal.Decimal. Les facteurs de conversion sont des float : convertir
    # explicitement la serie evite l'erreur ``Decimal * float``.
    df[quantity_col] = pd.to_numeric(df[quantity_col], errors="raise").astype("float64")
    unit_clean = df[unit_col].astype(str).str.strip().str.upper()
    multiplier = unit_clean.map(conversion_table)
    known = multiplier.notna()

    df.loc[known, quantity_col] = df.loc[known, quantity_col] * multiplier[known]
    df[unit_col] = unit_clean.where(~known, canonical_unit)
    return df


def _normalize_quantities(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise les deux couples quantite/unite (Quantity Unit et Quantity 2 Unit), famille par
    famille : seules les unites d'une meme famille physique (poids, volume, longueur, surface)
    peuvent fusionner entre elles. Une unite hors de toute famille connue reste inchangee.
    """
    for conversion_table, canonical_unit in UNIT_CONVERSION_FAMILIES:
        df = _normalize_unit_column(df, "Total Quantity", "Quantity Unit", conversion_table, canonical_unit)
        df = _normalize_unit_column(df, "Total Quantity 2", "Quantity 2 Unit", conversion_table, canonical_unit)
    return df


def _convert_snowflake_row_to_usd(row) -> tuple[float | None, str, bool]:
    """Convertit "Total Value" en USD pour une ligne agregee Snowflake (deja multiplie cote SQL).

    Retourne (valeur_usd_ou_None, reason, excluded_null_value).

    reason == "" si la conversion a reussi OU si la ligne a ete exclue silencieusement (voir
    excluded_null_value). excluded_null_value est True uniquement pour une ligne dont la
    conversion a echoue (valeur manquante, devise manquante/invalide, ou devise connue mais taux
    de change introuvable) alors que "Total Value" est nul ou zero : cette ligne ne represente
    aucune transaction reelle, donc son echec de conversion ne doit pas rendre tout le groupe
    NON_COMPARABLE (cf. _aggregate_snowflake). Elle contribue 0 au total et reste tracee via
    excluded_null_value_rows plutot que d'etre silencieusement ignoree sans trace.
    """
    value = row["Total Value"]
    value_is_negligible = pd.isna(value) or float(value) == 0.0

    def _fail(reason: str) -> tuple[float | None, str, bool]:
        if value_is_negligible:
            return 0.0, "", True
        return None, reason, False

    if pd.isna(value):
        return _fail("valeur_manquante")

    currency = row["Currency"]
    currency_clean = str(currency).strip().upper() if pd.notna(currency) else ""
    if not currency_clean or currency_clean == "N/A":
        return _fail("devise_manquante")

    if currency_clean == "USD":
        return float(value), "", False

    month = row["Month"]
    try:
        # MOP (pataca de Macao) : Frankfurter ne couvre pas cette devise, passage par le peg
        # fixe MOP->HKD (voir get_mop_to_usd_rate dans exchange_rate.py).
        if currency_clean == "MOP":
            rate_info = get_mop_to_usd_rate(month.year, month.month)
        else:
            rate_info = get_monthly_average_rate(currency_clean, month.year, month.month)
    except ExchangeRateError as exc:
        return _fail(f"{currency_clean}: {exc}")

    return float(value) * rate_info["rate"], "", False


def _aggregate_snowflake(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise les unites, convertit chaque ligne en USD, puis somme par GROUP_KEYS.

    Si au moins une devise du groupe echoue a se convertir *avec une valeur reelle* (non nulle),
    total_value_snowflake_usd est mis a NaN et value_reason renseigne (le groupe entier est
    NON_COMPARABLE cote valeur), plutot que de sommer partiellement les devises qui ont reussi
    (evite un total sous-evalue silencieux). Une ligne en echec de conversion mais dont
    "Total Value" est nul ou zero (ex. devise manquante sur une ligne sans transaction reelle)
    est exclue silencieusement de ce calcul de contamination : elle ne contribue de toute facon
    rien au total, comptee separement dans excluded_null_value_rows. Les quantites et
    Number of Records restent independants de la conversion de devise.
    """
    df = _normalize_quantities(df)

    converted = df.apply(_convert_snowflake_row_to_usd, axis=1, result_type="expand")
    df["_value_usd"] = converted[0]
    df["_convert_reason"] = converted[1]
    df["_excluded_null_value_records"] = converted[2].astype(bool) * df["Number of Records"]

    agg = (
        df.groupby(GROUP_KEYS, dropna=False)
        .agg(
            number_of_records_snowflake=("Number of Records", "sum"),
            total_quantity_snowflake=("Total Quantity", "sum"),
            total_quantity2_snowflake=("Total Quantity 2", "sum"),
            total_value_snowflake_usd=("_value_usd", "sum"),
            excluded_null_value_rows=("_excluded_null_value_records", "sum"),
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
    # Meme normalisation de type pour Value, utilisee ensuite dans des calculs
    # avec les valeurs USD converties (float) de l'autre source.
    df["Total Value"] = pd.to_numeric(df["Total Value"], errors="raise").astype("float64")
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
        snowflake_val = row[snowflake_col]
        clustered_val = row[clustered_col]
        if clustered_val == 0 and snowflake_val == 0:
            pct_diffs.append(0.0)
            statuses.append("MATCH")
            reasons.append("")
        elif clustered_val == 0:
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append(f"clustered_{metric_name}_zero")
        else:
            pct_diff = abs(snowflake_val - clustered_val) / abs(clustered_val)
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
        elif row["total_value_clustered_usd"] == 0 and row["total_value_snowflake_usd"] == 0:
            value_pct_diffs.append(0.0)
            value_statuses.append("MATCH")
            value_reasons.append("")
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
            "Quantity 2 Unit": matched["Quantity 2 Unit"],
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
            "excluded_null_value_rows": matched["excluded_null_value_rows"],
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
    unrecognized_units = set()

    for dataset in datasets:
        print(f"\n########## Dataset : {dataset} ##########")
        try:
            df_clustered = run_query(CLUSTERED_QUERY, params={"data_source": dataset})
            df_snowflake = run_query(_build_snowflake_query(dataset))
            for df, col in [
                (df_clustered, "Quantity Unit"),
                (df_clustered, "Quantity 2 Unit"),
                (df_snowflake, "Quantity Unit"),
                (df_snowflake, "Quantity 2 Unit"),
            ]:
                unrecognized_units |= _find_unrecognized_units(df, col)
            result = compare_aggregated(df_snowflake, df_clustered, tolerance=0.03)
            result.insert(0, "Dataset", dataset)
            all_results.append(result)
        except Exception as exc:
            print(f"[ERREUR] Dataset '{dataset}' ignore : {exc}")
            failed_datasets.append(dataset)

    if unrecognized_units:
        print(
            f"\n[ATTENTION] Unites rencontrees non couvertes par une famille de conversion et non "
            f"reconnues comme unites de comptage (a verifier manuellement) : {sorted(unrecognized_units)}"
        )

    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        combined.to_csv("aggregated_comparison_result.csv", index=False)
        print(
            f"\nResultat exporte dans aggregated_comparison_result.csv "
            f"({len(combined)} lignes, {len(all_results)} dataset(s))"
        )

        mismatch_mask = (
            (combined["status_quantity"] == "MISMATCH")
            | (combined["status_quantity2"] == "MISMATCH")
            | (combined["status_value"] == "MISMATCH")
        )
        mismatches_only = combined[mismatch_mask]
        mismatches_only.to_csv("aggregated_comparison_mismatches_only.csv", index=False)
        print(
            f"Mismatchs exportes dans aggregated_comparison_mismatches_only.csv "
            f"({len(mismatches_only)} ligne(s))"
        )

    if failed_datasets:
        print(f"\nDatasets en echec (ignores) : {failed_datasets}")
