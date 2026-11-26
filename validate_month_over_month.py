"""Controle qualite : variation d'un mois a l'autre (month-over-month) sur les donnees Clustered.

Pour chaque serie mensuelle (une par combinaison des colonnes `group_by`, par defaut pays Reporter
x sens TradeFlow), chaque mois est compare au mois SUIVANT s'il existe, sinon au mois PRECEDENT :

    Difference = (Value - Reference value) / Reference value

    |Difference| <= seuil  -> PASS
    |Difference| >  seuil  -> FAIL             anomalie potentielle a investiguer
    aucun mois adjacent    -> NOT ENOUGH DATA  ni M+1 ni M-1 dans la serie
    Reference value = 0    -> PASS si Value vaut aussi 0 (aucune variation), sinon REFERENCE ZERO
                              (variation infinie : signalee a part, sans division par zero)

"Mois suivant / precedent" = mois calendaire adjacent (M+1 / M-1) : un mois separe par un trou
n'est pas adjacent. Le seuil, les datasets par defaut, le niveau des series et les mois a valider
(start_month : "latest" = dernier mois de chaque serie, N = ses N derniers mois, "YYYY-MM", ou
vide = tout l'historique) se reglent dans MOM_CONFIG_FILE. Source : table Clustered (valeurs deja
en USD), un dataset = un DataSource.

Usage :
    python validate_month_over_month.py                      # datasets de MOM_CONFIG_FILE
    python validate_month_over_month.py <dataset> [...]      # ces datasets
    python validate_month_over_month.py all                  # tous les DataSources de Clustered
    python validate_month_over_month.py --config autre.yaml  # autre fichier de configuration
    python validate_month_over_month.py <dataset> --start-month 18   # ses 18 derniers mois
"""

from __future__ import annotations

import argparse

import pandas as pd
import yaml

from snowflake_client import run_query

MOM_CONFIG_FILE = "mom_validation_config.yaml"
CLUSTERED_TABLE = 'DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED"'
OUTPUT_FILE = "mom_validation_result.csv"
ANOMALIES_FILE = "mom_validation_anomalies.csv"

PASS, FAIL, NOT_ENOUGH_DATA, REFERENCE_ZERO = "PASS", "FAIL", "NOT ENOUGH DATA", "REFERENCE ZERO"

# Valeur de start_month : ne valider que le dernier mois de chaque serie (compare au precedent).
LATEST = "latest"
ANOMALY_STATUSES = (FAIL, REFERENCE_ZERO)

# Colonnes autorisees dans group_by -> expression SQL. Liste blanche : ces expressions sont
# interpolees dans la requete, jamais une valeur libre lue dans le fichier de configuration.
GROUP_BY_COLUMNS = {
    "Reporter": """COALESCE(NULLIF(TRIM(TO_VARCHAR("Reporter")), ''), 'N/A')""",
    "Partner": """COALESCE(NULLIF(TRIM(TO_VARCHAR("Partner")), ''), 'N/A')""",
    "TradeFlow": """COALESCE(NULLIF(TRIM(TO_VARCHAR("TradeFlow")), ''), 'N/A')""",
    "HSChapter": """COALESCE(NULLIF(LEFT(REGEXP_REPLACE(TO_VARCHAR("HSCode"), '[^0-9]', ''), 2), ''), 'N/A')""",
}

RESULT_COLUMNS = ["Month", "Value", "Reference month", "Reference value", "Difference", "difference_ratio", "Status"]


def load_config(path: str = MOM_CONFIG_FILE) -> dict:
    """Lit et valide la configuration : threshold (fraction, 0.05 = 5 %), datasets, group_by,
    start_month. Une valeur invalide arrete le script avec un message explicite."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise SystemExit(f"Fichier de configuration introuvable : {path}")

    threshold = cfg.get("threshold", 0.05)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold < 1:
        raise SystemExit(
            f"threshold invalide dans {path} : {threshold!r}. Attendu une fraction entre 0 et 1 "
            "(0.05 pour 5 %)."
        )

    group_by = cfg.get("group_by") or ["Reporter", "TradeFlow"]
    if isinstance(group_by, str):
        group_by = [group_by]
    unknown = [c for c in group_by if c not in GROUP_BY_COLUMNS]
    if unknown:
        raise SystemExit(f"group_by inconnu(s) dans {path} : {unknown}. Possibles : {list(GROUP_BY_COLUMNS)}")

    start_month, last_months = parse_start_month(cfg.get("start_month"), where=path)

    datasets = cfg.get("datasets")
    if isinstance(datasets, str):
        datasets = [datasets]
    return {"threshold": float(threshold), "group_by": list(group_by), "start_month": start_month,
            "last_months": last_months, "datasets": datasets}


def parse_start_month(value, where: str = "la configuration") -> tuple[str | None, int | None]:
    """Interprete start_month (fichier de config ou --start-month). Renvoie (start_month, last_months) :
      vide / "all"  -> (None, None)       tout l'historique ;
      "latest"      -> (None, 1)          dernier mois de chaque serie ;
      N (ex: 18)    -> (None, N)          les N derniers mois calendaires de chaque serie ;
      "YYYY-MM"     -> ("YYYY-MM", None)  tous les mois a partir de celui-ci.
    """
    if value is None or str(value).strip().lower() in ("", "all"):
        return None, None
    text = str(value).strip().lower()
    if text == LATEST:
        return None, 1
    if isinstance(value, bool):
        pass
    elif text.isdigit():
        if int(text) >= 1:
            return None, int(text)
    else:
        try:
            return str(pd.Period(text, freq="M")), None
        except ValueError:
            pass
    raise SystemExit(
        f"start_month invalide ({where}) : {value!r}. Attendu : latest, un nombre de mois (ex: 18), \"YYYY-MM\" ou vide."
    )


def validate_series(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Valide une serie mensuelle (colonnes Month, Value) selon les regles du module.

    Plusieurs lignes du meme mois sont additionnees. Renvoie une ligne par mois, dans l'ordre
    chronologique, avec les colonnes RESULT_COLUMNS : "Difference" est le pourcentage affiche
    ("+42.86%"), difference_ratio la meme valeur en fraction (0.4286) pour trier ou filtrer.
    """
    if df.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    periods = pd.PeriodIndex(pd.to_datetime(df["Month"]), freq="M")
    values = pd.to_numeric(df["Value"], errors="raise").astype("float64").groupby(periods).sum().sort_index()

    rows = []
    for period, value in values.items():
        if period + 1 in values.index:
            ref_period = period + 1
        elif period - 1 in values.index:
            ref_period = period - 1
        else:
            ref_period = None

        row = {"Month": str(period), "Value": value, "Reference month": None, "Reference value": None,
               "Difference": "", "difference_ratio": None}
        if ref_period is None:
            rows.append({**row, "Status": NOT_ENOUGH_DATA})
            continue

        reference = values[ref_period]
        row.update({"Reference month": str(ref_period), "Reference value": reference})
        if reference == 0:
            if value == 0:
                rows.append({**row, "Difference": "+0.00%", "difference_ratio": 0.0, "Status": PASS})
            else:
                rows.append({**row, "Status": REFERENCE_ZERO})
            continue

        ratio = (value - reference) / abs(reference)
        # Tolerance infime pour qu'un ecart d'exactement 5 % reste PASS malgre l'arrondi flottant.
        status = PASS if abs(ratio) <= threshold + 1e-12 else FAIL
        rows.append({**row, "Difference": f"{ratio * 100:+.2f}%", "difference_ratio": round(ratio, 6), "Status": status})

    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def validate_dataset(df: pd.DataFrame, group_by: list[str], threshold: float) -> pd.DataFrame:
    """Applique validate_series a chaque serie (combinaison de group_by) d'un dataset."""
    results = []
    for keys, series in df.groupby(group_by, dropna=False, sort=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        result = validate_series(series[["Month", "Value"]], threshold)
        for col, key in reversed(list(zip(group_by, keys))):
            result.insert(0, col, key)
        results.append(result)
    if not results:
        return pd.DataFrame(columns=group_by + RESULT_COLUMNS)
    return pd.concat(results, ignore_index=True)


def keep_last_months(result: pd.DataFrame, group_by: list[str], n_months: int) -> pd.DataFrame:
    """Ne garde que les n_months derniers mois calendaires de chaque serie (1 = dernier mois seul).

    La validation a deja ete faite sur toute la serie : chaque mois garde sa reference (suivant,
    sinon precedent), meme si elle tombe hors de la fenetre. Le dernier mois, sans suivant, est
    compare au precedent. Chaque serie a sa propre fenetre, finissant a son propre dernier mois,
    pour qu'une serie qui n'est plus alimentee reste visible (mois anciens) au lieu de disparaitre.
    """
    if result.empty:
        return result
    month_index = result["Month"].str[:4].astype(int) * 12 + result["Month"].str[5:7].astype(int)
    latest = month_index.groupby([result[c] for c in group_by], dropna=False).transform("max")
    return result[month_index > latest - n_months].reset_index(drop=True)


def fetch_monthly_values(dataset: str, group_by: list[str], start_month: str | None) -> pd.DataFrame:
    """Totaux mensuels de "Value" (USD) dans Clustered pour un DataSource, par colonnes group_by,
    a partir de start_month ("YYYY-MM") ou sur tout l'historique si None."""
    select_groups = ",\n    ".join(f'{GROUP_BY_COLUMNS[c]} AS "{c}"' for c in group_by)
    params = {"data_source": dataset}
    start_filter = ""
    if start_month:
        start_filter = 'AND "DateReporter"::DATE >= TO_DATE(%(start_month)s)'
        params["start_month"] = f"{start_month}-01"
    query = f"""
SELECT
    {select_groups},
    DATE_TRUNC('MONTH', "DateReporter"::DATE) AS "Month",
    SUM(COALESCE("Value", 0)) AS "Value"
FROM {CLUSTERED_TABLE}
WHERE "DataSource" = %(data_source)s {start_filter}
GROUP BY {", ".join(str(i) for i in range(1, len(group_by) + 2))}
"""
    df = run_query(query, params=params)
    df["Value"] = pd.to_numeric(df["Value"], errors="raise").astype("float64")
    return df


def list_all_datasets() -> list[str]:
    """Tous les DataSources presents dans Clustered."""
    df = run_query(f'SELECT DISTINCT "DataSource" FROM {CLUSTERED_TABLE} ORDER BY "DataSource"')
    return df["DataSource"].dropna().astype(str).tolist()


def main() -> None:
    parser = argparse.ArgumentParser(description="Controle de variation d'un mois a l'autre sur les donnees Clustered.")
    parser.add_argument("datasets", nargs="*", help="DataSource(s) a valider, ou 'all' (defaut : ceux du fichier de config).")
    parser.add_argument("--config", default=MOM_CONFIG_FILE, help=f"Fichier de configuration (defaut : {MOM_CONFIG_FILE}).")
    parser.add_argument("--start-month", help="Remplace start_month de la config : latest, un nombre de mois (ex: 18), YYYY-MM ou all.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.start_month is not None:
        cfg["start_month"], cfg["last_months"] = parse_start_month(args.start_month, where="--start-month")
    datasets = args.datasets or cfg["datasets"]
    if not datasets:
        raise SystemExit(f"Aucun dataset : passe-le en argument ou renseigne 'datasets' dans {args.config}.")
    if len(datasets) == 1 and str(datasets[0]).strip().lower() == "all":
        datasets = list_all_datasets()

    if cfg["last_months"] == 1:
        months_label = "dernier mois de chaque serie"
    elif cfg["last_months"]:
        months_label = f"{cfg['last_months']} derniers mois de chaque serie"
    elif cfg["start_month"]:
        months_label = f"a partir de {cfg['start_month']}"
    else:
        months_label = "tout l'historique"
    print(
        f"Seuil : {cfg['threshold']:.2%} | series par : {', '.join(cfg['group_by'])} | "
        f"mois valides : {months_label} | {len(datasets)} dataset(s)"
    )

    all_results, failed = [], []
    for dataset in datasets:
        print(f"\n########## Dataset : {dataset} ##########")
        try:
            df = fetch_monthly_values(dataset, cfg["group_by"], cfg["start_month"])
            result = validate_dataset(df, cfg["group_by"], cfg["threshold"])
            if cfg["last_months"]:
                result = keep_last_months(result, cfg["group_by"], cfg["last_months"])
        except Exception as exc:
            print(f"[ERREUR] Dataset '{dataset}' ignore : {exc}")
            failed.append(dataset)
            continue
        result.insert(0, "Dataset", dataset)
        all_results.append(result)
        n_series = len(result[cfg["group_by"]].drop_duplicates()) if len(result) else 0
        print(f"{n_series} serie(s), {len(result)} mois : {result['Status'].value_counts().to_dict()}")

    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        combined.to_csv(OUTPUT_FILE, index=False)
        anomalies = combined[combined["Status"].isin(ANOMALY_STATUSES)]
        anomalies.to_csv(ANOMALIES_FILE, index=False)
        print(f"\nResultat exporte dans {OUTPUT_FILE} ({len(combined)} ligne(s))")
        print(f"Anomalies ({' / '.join(ANOMALY_STATUSES)}) exportees dans {ANOMALIES_FILE} ({len(anomalies)} ligne(s))")
        print("\n=== Resume ===")
        for status in (PASS, FAIL, REFERENCE_ZERO, NOT_ENOUGH_DATA):
            n = int((combined["Status"] == status).sum())
            print(f"  {status:16s}: {n} ({n / len(combined) * 100 if len(combined) else 0:.2f}%)")
    if failed:
        print(f"\nDatasets en echec (ignores) : {failed}")


if __name__ == "__main__":
    main()
