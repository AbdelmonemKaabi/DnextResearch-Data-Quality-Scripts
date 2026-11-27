"""Verification ciblee Clustered vs Trading Economics sur une liste de (dataset, mois, direction)
lue dans un fichier Excel (ou CSV), au lieu de tout l'historique de tous les pays
(compare_all_clustered_vs_tradingeconomics.py).

Sert a controler des mois precis (ex: les MISMATCH d'une comparaison precedente, ou un mois
corrige) sans relancer la comparaison complete. Clustered est interroge en direct ; la serie
Trading Economics vient du cache te_cache/ (partage avec les autres scripts), --refresh la re-scrape.

Fichier d'entree, une ligne par verification, colonnes (casse ignoree) :
    Dataset          DataSource Clustered, ex: dnexr-all-eth-tf-customs-bilateral-monthly
    Month            AAAA-MM (texte ou date Excel)
    Trade direction  Export ou Import

Le pays n'est pas dans le fichier : c'est le Reporter que Clustered associe au dataset (meme regle
que compare_dataset_vs_tradingeconomics.py). Un dataset multi-pays est NON_COMPARABLE
(pays_indetermine).

Usage :
    python check_months_vs_tradingeconomics.py                    # fichier INPUT_FILE
    python check_months_vs_tradingeconomics.py ma_liste.xlsx
    python check_months_vs_tradingeconomics.py ma_liste.xlsx --refresh
"""

from __future__ import annotations

import argparse
import os

import pandas as pd

from compare_aggregated_sources import EXCEL_SIGNED_PERCENT_FORMAT, with_percent_text
from compare_all_clustered_vs_tradingeconomics import COMPARED_FLOWS, _load_or_scrape_te
from compare_clustered_vs_tradingeconomics import TOLERANCE, resolve_te_slug
from compare_dataset_vs_tradingeconomics import country_from_name
from snowflake_client import run_query

# Fichier de verifications lu quand aucun chemin n'est passe en ligne de commande (bouton Run de
# l'editeur). Chemin relatif au dossier du terminal, ou absolu (ex: r"C:\Users\...\liste.xlsx").
INPUT_FILE = r"C:\Users\Abdelmonem\Downloads\Customs datasets – potential outlier months.xlsx"
OUTPUT_FILE = "month_checks_result.xlsx"
INPUT_COLUMNS = {"dataset": "Dataset", "month": "Month", "trade direction": "Trade Direction"}

# Deux requetes pour tout le fichier (et non deux par dataset) : la table Clustered est lue une
# seule fois par requete, quel que soit le nombre de datasets a verifier.
COUNTRIES_QUERY = """
SELECT DISTINCT "DataSource", TRIM(TO_VARCHAR("Reporter")) AS "Country"
FROM DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED"
WHERE "DataSource" IN ({datasets})
"""

TOTALS_QUERY = """
SELECT
    "DataSource",
    TRIM(TO_VARCHAR("Reporter")) AS "Country",
    DATE_TRUNC('MONTH', "DateReporter"::DATE) AS "Month",
    TRIM(TO_VARCHAR("TradeFlow")) AS "Trade Direction",
    SUM(COALESCE("Value", 0)) AS "Total Value"
FROM DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED"
WHERE "DataSource" IN ({datasets})
  AND DATE_TRUNC('MONTH', "DateReporter"::DATE) IN ({months})
GROUP BY 1, 2, 3, 4
"""


def parse_month(value) -> pd.Timestamp:
    """Premier jour du mois, a partir de "2026-09", "2026-09-01" ou d'une date Excel."""
    return pd.Period(str(value).strip(), freq="M").to_timestamp()


def load_checks(path: str) -> pd.DataFrame:
    """Lit le fichier de verifications : colonnes Dataset, Month, Trade Direction normalisees (les
    autres colonnes du fichier sont gardees pour la sortie), lignes vides ignorees, doublons
    retires. Leve ValueError si une colonne obligatoire manque.

    Export/Import sont normalises en casse ; les autres flux (RE_IMP, REEX, DEXP...) restent tels
    quels et sortent en NON_COMPARABLE, Trading Economics ne publiant que Export et Import."""
    df = pd.read_csv(path) if path.lower().endswith(".csv") else pd.read_excel(path)
    df = df.rename(columns={c: INPUT_COLUMNS.get(str(c).strip().lower(), c) for c in df.columns})
    missing = [c for c in INPUT_COLUMNS.values() if c not in df.columns]
    if missing:
        raise ValueError(f"{path} : colonne(s) manquante(s) {missing} (attendu : {list(INPUT_COLUMNS.values())})")

    # Seules les colonnes cles sont lues en texte : les autres (ex: "Value (bn)") gardent leur type
    # pour rester numeriques dans le fichier Excel de sortie.
    df = df.dropna(how="all", subset=list(INPUT_COLUMNS.values()))
    for column in INPUT_COLUMNS.values():
        df[column] = df[column].astype(str)
    df["Dataset"] = df["Dataset"].str.strip()
    df["Month"] = df["Month"].map(parse_month)
    direction = df["Trade Direction"].str.strip()
    df["Trade Direction"] = direction.where(~direction.str.title().isin(COMPARED_FLOWS), direction.str.title())
    return df.drop_duplicates(subset=list(INPUT_COLUMNS.values())).reset_index(drop=True)


def _placeholders(prefix: str, values: list, params: dict) -> str:
    """Ajoute `values` a `params` (cles prefix_0, prefix_1...) et renvoie "%(prefix_0)s, ..."."""
    for i, value in enumerate(values):
        params[f"{prefix}_{i}"] = value
    return ", ".join(f"%({prefix}_{i})s" for i in range(len(values)))


def fetch_dataset_countries(datasets: list[str]) -> dict[str, list[str]]:
    """Codes Reporter de chaque dataset dans Clustered ; a defaut (dataset absent de Clustered),
    le code ISO3 lu dans le nom du dataset -- meme regle que compare_dataset_vs_tradingeconomics.py."""
    params: dict = {}
    df = run_query(COUNTRIES_QUERY.format(datasets=_placeholders("dataset", datasets, params)), params=params)
    df = df[df["Country"].fillna("").str.len() > 0]
    found = df.groupby("DataSource")["Country"].apply(lambda s: sorted(set(s))).to_dict()
    return {ds: found.get(ds) or country_from_name(ds) for ds in datasets}


def fetch_clustered_totals(datasets: list[str], months: list[pd.Timestamp]) -> pd.DataFrame:
    """Totaux Clustered par (DataSource, Country, Month, Trade Direction) pour tous les datasets et
    mois du fichier, en une requete."""
    params: dict = {}
    query = TOTALS_QUERY.format(
        datasets=_placeholders("dataset", datasets, params),
        months=_placeholders("month", [m.date() for m in months], params),
    )
    df = run_query(query, params=params)
    df["Month"] = pd.to_datetime(df["Month"])
    df["Total Value"] = pd.to_numeric(df["Total Value"], errors="raise").astype("float64")
    return df


def compare_checks(checks: pd.DataFrame, clustered: pd.DataFrame, te_df: pd.DataFrame) -> pd.DataFrame:
    """Une ligne par (Month, Trade Direction) demande, meme si une des sources n'a pas ce mois.

    ratio_te_clustered = TE / Clustered : un ratio proche de 1000 ou 0,001 signale une valeur
    chargee dans la mauvaise echelle (milliers, millions) plutot qu'un vrai ecart.
    """
    te = te_df.assign(Month=pd.to_datetime(te_df["Month"]))
    rows = []
    for _, check in checks.iterrows():
        month, direction = check["Month"], check["Trade Direction"]
        cl = clustered[(clustered["Month"] == month) & (clustered["Trade Direction"] == direction)]
        te_points = te[(te["Month"] == month) & (te["Direction"] == direction)]
        value_cl = float(cl["Total Value"].sum()) if len(cl) else None
        # Deux points TE dans le meme mois (provisoire puis definitif) : le dernier du graphique est
        # garde, meme regle que _compare_country (compare_clustered_vs_tradingeconomics.py).
        value_te = float(te_points["value_usd"].iloc[-1]) if len(te_points) else None

        ratio = pct_diff = None
        if value_cl is None:
            status, reason = "NON_COMPARABLE", "absent_clustered"
        elif len(te_points) == 0:
            status, reason = "NON_COMPARABLE", "absent_te"
        elif pd.isna(value_te):
            status, reason = "NON_COMPARABLE", "te_conversion_echouee"
        elif value_cl == 0:
            status, reason = ("MATCH", "") if value_te == 0 else ("NON_COMPARABLE", "clustered_value_zero")
        else:
            ratio = value_te / value_cl
            pct_diff = (value_te - value_cl) / abs(value_cl)  # signe : positif quand TE est au-dessus
            status, reason = ("MATCH" if abs(pct_diff) <= TOLERANCE else "MISMATCH"), ""
        if len(te_points) > 1 and reason == "":
            reason = "te_mois_en_double_dernier_point_garde"
        rows.append({
            "Month": month.date(), "Trade Direction": direction,
            "value_clustered_usd": value_cl, "value_te_usd": value_te,
            "ratio_te_clustered": ratio, "pct_diff": pct_diff, "status": status, "reason": reason,
        })
    return pd.DataFrame(rows)


def _not_comparable(checks: pd.DataFrame, reason: str) -> pd.DataFrame:
    """Toutes les verifications d'un dataset en NON_COMPARABLE avec la meme raison."""
    return checks[["Month", "Trade Direction"]].assign(
        Month=checks["Month"].dt.date, status="NON_COMPARABLE", reason=reason
    )


def check_dataset(dataset: str, checks: pd.DataFrame, codes: list[str], clustered: pd.DataFrame,
                  refresh: bool, te_cache: dict) -> pd.DataFrame:
    """Verifications d'un dataset : pays (codes), serie TE (cache), puis comparaison avec ses totaux
    Clustered (`clustered`, deja filtres sur le dataset)."""
    if len(codes) != 1:
        print(f"[ATTENTION] {dataset} : pays indetermine ({codes or 'aucun'}).")
        return _not_comparable(checks, "pays_indetermine").assign(Country=", ".join(codes))
    code = codes[0]
    slug, reason = resolve_te_slug(code)
    if slug is None:
        return _not_comparable(checks, reason).assign(Country=code)

    print(f"\n########## {dataset} | {code} -> {slug} | {len(checks)} verification(s) ##########")
    if slug not in te_cache:
        try:
            te_cache[slug], scraped = _load_or_scrape_te(slug, refresh)
            print("Trading Economics : " + ("scrape" if scraped else "cache"))
        except Exception as exc:
            print(f"[ERREUR] Trading Economics {slug} : {exc}")
            te_cache[slug] = None
    if te_cache[slug] is None:
        return _not_comparable(checks, "te_echec").assign(Country=code)
    if te_cache[slug].empty:
        # Ex: Ethiopie, pas de lignes Exports/Imports dans la table Trading Economics.
        return _not_comparable(checks, "te_vide").assign(Country=code)

    return compare_checks(checks, clustered[clustered["Country"] == code], te_cache[slug]).assign(Country=code)


def save_excel(df: pd.DataFrame, path: str) -> None:
    """Ecrit le resultat en Excel : filtres et en-tete fige, montants USD avec separateurs de
    milliers, ecart en pourcentage, largeurs de colonnes ajustees."""
    from openpyxl.utils import get_column_letter

    number_formats = {
        "value_clustered_usd": "#,##0", "value_te_usd": "#,##0",
        "ratio_te_clustered": "0.000", "pct_diff": EXCEL_SIGNED_PERCENT_FORMAT,
    }
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Resultat")
        ws = writer.sheets["Resultat"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, column in enumerate(df.columns, start=1):
            letter = get_column_letter(i)
            width = max([len(str(column))] + [len(str(v)) for v in df[column].head(500) if pd.notna(v)])
            ws.column_dimensions[letter].width = min(width + 2, 60)
            if column in number_formats:
                ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width, 18)
                for cell in ws[letter][1:]:
                    cell.number_format = number_formats[column]


def main() -> None:
    parser = argparse.ArgumentParser(description="Clustered vs Trading Economics sur une liste de (dataset, mois, direction).")
    parser.add_argument("file", nargs="?", default=INPUT_FILE,
                        help=f"Fichier Excel ou CSV avec les colonnes Dataset, Month, Trade direction (defaut : {INPUT_FILE}).")
    parser.add_argument("--refresh", action="store_true", help="Ignore le cache TE et re-scrape les pays demandes.")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        raise SystemExit(f"Fichier introuvable : {args.file}")
    checks = load_checks(args.file)
    comparable = checks[checks["Trade Direction"].isin(COMPARED_FLOWS)]
    print(f"{len(checks)} verification(s) sur {checks['Dataset'].nunique()} dataset(s), "
          f"dont {len(checks) - len(comparable)} sur un flux non publie par Trading Economics.")

    results = []
    if len(comparable):
        datasets = list(comparable["Dataset"].unique())
        countries = fetch_dataset_countries(datasets)
        totals = fetch_clustered_totals(datasets, sorted(comparable["Month"].unique()))
        print(f"Clustered : {len(totals)} total(aux) mensuel(s) recupere(s) pour {len(datasets)} dataset(s).")
        te_cache: dict = {}
        for dataset, dataset_checks in comparable.groupby("Dataset", sort=False):
            result = check_dataset(dataset, dataset_checks, countries[dataset],
                                   totals[totals["DataSource"] == dataset], args.refresh, te_cache)
            results.append(result.assign(Dataset=dataset))

    keys = list(INPUT_COLUMNS.values())
    result_columns = ["Country", "value_clustered_usd", "value_te_usd", "ratio_te_clustered", "pct_diff", "status", "reason"]
    found = pd.concat(results, ignore_index=True) if results else pd.DataFrame(columns=keys + result_columns)
    found["Month"] = pd.to_datetime(found["Month"])
    # Jointure sur le fichier d'entree : meme ordre de lignes, colonnes d'origine (Type, Severity...) gardees.
    combined = checks.merge(found[keys + result_columns], on=keys, how="left")
    other_flow = ~combined["Trade Direction"].isin(COMPARED_FLOWS)
    combined.loc[other_flow, "status"] = "NON_COMPARABLE"
    combined.loc[other_flow, "reason"] = "flux_non_publie_te"
    combined["Month"] = combined["Month"].dt.strftime("%Y-%m")
    combined = combined[keys + result_columns + [c for c in checks.columns if c not in keys]]

    save_excel(combined, OUTPUT_FILE)
    with pd.option_context("display.width", 250, "display.max_columns", None, "display.float_format", "{:,.4f}".format):
        print("\n" + with_percent_text(combined[keys + result_columns]).to_string(index=False))
    print(f"\nResultat exporte dans {OUTPUT_FILE} ({len(combined)} ligne(s)) : {combined['status'].value_counts().to_dict()}")


if __name__ == "__main__":
    main()
