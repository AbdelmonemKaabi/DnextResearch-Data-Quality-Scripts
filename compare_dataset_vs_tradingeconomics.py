"""Comparaison des totaux mensuels d'un dataset source (table Snowflake brute) avec Trading
Economics, sens par sens (Export/Import).

Variante de compare_all_clustered_vs_tradingeconomics.py qui contourne Clustered : la valeur est lue
dans la table du dataset (meme nom que le DataSource Clustered), multipliee par "Value Unit
multiplier". Permet de savoir si un ecart Clustered vs Trading Economics vient deja de la source
ou de son chargement dans Clustered.

Devise de comparaison, decidee mois par mois (colonne compared_in) :
  - toutes les valeurs du mois sont dans la devise de Trading Economics (ex: TND pour la Tunisie)
    -> comparaison directe dans cette devise, sans conversion (aucun taux de change en jeu) ;
  - sinon (autre devise, ou plusieurs devises dans le mois) -> tout est converti en USD avec la
    meme conversion que compare_aggregated_sources.py (_convert_snowflake_row_to_usd : parite fixe,
    Frankfurter, puis currency-api), et compare a la serie TE convertie en USD.
Les devises presentes dans le dataset sont listees par mois (dataset_currencies) et, sur tout
l'historique, dans le resume ; les points a verifier (plusieurs devises, devise manquante, devise
differente de TE, conversion impossible, ratio aberrant) sont regroupes dans la colonne notes.

Le pays du dataset est le Reporter que Clustered associe a ce DataSource : un dataset multi-pays
(plusieurs Reporters, ex: Eurostat) n'a pas de serie Trading Economics unique et est ignore.

Comme pour compare_aggregated_sources.py, seules les lignes avec un code HS numerique sont
gardees, et les codes DEXP/REEX sont ramenes a "Export". Seuls les mois couverts par la serie
Trading Economics sont convertis (evite des centaines d'appels de taux de change inutiles).

Usage :
    python compare_dataset_vs_tradingeconomics.py dnexr-all-che-tf-customs-bilateral-monthly
    python compare_dataset_vs_tradingeconomics.py ds1 ds2
    python compare_dataset_vs_tradingeconomics.py all        # tous les DataSources de Clustered (hors
                                                             # Eurostat / UN Comtrade multi-pays)
    python compare_dataset_vs_tradingeconomics.py            # datasets de datasets_config.yaml
    python compare_dataset_vs_tradingeconomics.py --refresh  # ignore le cache TE, re-scrape

Choisir la periode (par defaut tout l'historique) :
    python compare_dataset_vs_tradingeconomics.py <dataset> --month 2025-03            # un mois
    python compare_dataset_vs_tradingeconomics.py <dataset> --month 2025-03 2025-04    # plusieurs mois
    python compare_dataset_vs_tradingeconomics.py <dataset> --year 2025                # annee complete
    python compare_dataset_vs_tradingeconomics.py ds1 ds2 ds3 --from 2025-01 --to 2025-06   # intervalle
    python compare_dataset_vs_tradingeconomics.py ds1 ds2 --year 2025 --direction Export
    python compare_dataset_vs_tradingeconomics.py --file ma_liste.xlsx                 # liste dans un fichier
Fichier (--file, Excel ou CSV) : une ligne par demande, colonnes Dataset, puis Year + Month (ex: 2025
et 3 ; Year seul = annee complete), ou From + To (intervalle AAAA-MM), ou rien (tout l'historique),
et Trade Direction (optionnelle, vide = les deux). Voir load_selection_file.
Sorties : <output>_result.xlsx (feuilles Resume et Detail), <output>_result.csv, <output>_summary.csv,
avec --output dataset_vs_tradingeconomics par defaut.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import unicodedata

import pandas as pd
import pycountry

from compare_aggregated_sources import (
    EXCEL_SIGNED_PERCENT_FORMAT,
    TRADE_DIRECTION_ALIASES,
    _SAFE_DATASET_NAME_RE,
    _convert_snowflake_row_to_usd,
    _list_all_datasets,
    _resolve_datasets,
    percent_text,
    with_percent_text,
)
from compare_all_clustered_vs_tradingeconomics import (
    COMPARED_FLOWS,
    EXCLUDED_DATASOURCES,
    _is_te_monthly,
    _load_or_scrape_te,
    _summarize_pair,
)
from compare_clustered_vs_tradingeconomics import COUNTRY_CODES_QUERY, TOLERANCE, _compare_country, resolve_te_slug
from snowflake_client import run_query


# Devises "vides" : une valeur non nulle avec une de ces devises ne peut etre ni comparee en devise
# locale ni convertie, elle est signalee dans les notes.
MISSING_CURRENCIES = {"", "N/A", "NA", "NAN", "NONE"}

# Ratio TE / dataset au-dela duquel (ou en deca de son inverse) un mois est signale : un tel
# ecart vient d'une erreur d'echelle (milliers, millions) ou de devise, pas d'une revision.
ABERRANT_RATIO = 3.0

DATASET_MONTHLY_QUERY_TEMPLATE = """
WITH BASE_DATA AS (
    SELECT
        DATE_TRUNC('MONTH', "Date Reporter"::DATE) AS "Month",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Trade Direction")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Trade Direction")), ''), 'N/A') AS "Trade Direction",
        COALESCE(GET(TRY_PARSE_JSON(TO_VARCHAR("Currency")), 'Code')::STRING, NULLIF(TRIM(TO_VARCHAR("Currency")), ''), 'N/A') AS "Currency",
        "Value" * COALESCE("Value Unit multiplier", 1) AS "Value"
    FROM "{table}"
    WHERE NULLIF(REGEXP_REPLACE(SPLIT_PART(TRIM(TO_VARCHAR("HS Code")), '.', 1), '[^0-9]', ''), '') IS NOT NULL
      {date_filter}
)
SELECT "Month", "Trade Direction", "Currency", SUM("Value") AS "Total Value"
FROM BASE_DATA
GROUP BY "Month", "Trade Direction", "Currency"
"""

# Colonnes du fichier de selection (--file), casse et accents ignores. Seule Dataset est obligatoire.
SELECTION_COLUMNS = {
    "dataset": "Dataset", "dataset code": "Dataset", "datasource": "Dataset", "code": "Dataset",
    "year": "Year", "annee": "Year",
    "month": "Month", "mois": "Month",
    "from": "From", "start": "From", "du": "From", "debut": "From",
    "to": "To", "end": "To", "au": "To", "fin": "To",
    "trade direction": "Trade Direction", "direction": "Trade Direction", "sens": "Trade Direction",
}

# Noms de mois acceptes dans la colonne Month (anglais / francais, complets ou abreges, sans accents).
MONTH_NAMES = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan", "janvier", "janv"), ("february", "feb", "fevrier", "fev", "fevr"),
            ("march", "mar", "mars"), ("april", "apr", "avril", "avr"), ("may", "mai"),
            ("june", "jun", "juin"), ("july", "jul", "juillet", "juil"), ("august", "aug", "aout"),
            ("september", "sep", "sept", "septembre"), ("october", "oct", "octobre"),
            ("november", "nov", "novembre"), ("december", "dec", "decembre"),
        ],
        start=1,
    )
    for name in names
}


def _fetch_dataset_totals(dataset: str, start: dt.date | None = None, end: dt.date | None = None) -> pd.DataFrame:
    """Totaux mensuels du dataset brut par (Month, Trade Direction, Currency), en devise d'origine.
    Avec start/end, seuls les mois de [start, end[ sont lus (requete plus rapide)."""
    if not _SAFE_DATASET_NAME_RE.match(dataset):
        raise ValueError(f"Nom de dataset invalide (caracteres non autorises) : {dataset!r}")
    if start is None:
        return run_query(DATASET_MONTHLY_QUERY_TEMPLATE.format(table=dataset, date_filter=""))
    date_filter = 'AND "Date Reporter"::DATE >= TO_DATE(%(start)s) AND "Date Reporter"::DATE < TO_DATE(%(end)s)'
    query = DATASET_MONTHLY_QUERY_TEMPLATE.format(table=dataset, date_filter=date_filter)
    return run_query(query, params={"start": start.isoformat(), "end": end.isoformat()})


def parse_period(value) -> list[dt.date]:
    """Mois demandes, au 1er du mois : "2025-03" -> mars 2025 ; "2025" -> les 12 mois de 2025 ;
    une date Excel -> son mois. Leve ValueError sinon."""
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return [pd.Timestamp(value).to_period("M").to_timestamp().date()]
    text = str(value).strip()
    if text.endswith(".0"):  # annee lue comme nombre dans Excel (2025.0)
        text = text[:-2]
    if re.fullmatch(r"\d{4}", text):
        return [dt.date(int(text), month, 1) for month in range(1, 13)]
    try:
        return [pd.Period(text, freq="M").to_timestamp().date()]
    except ValueError:
        raise ValueError(f"Periode invalide : {value!r} (attendu AAAA-MM pour un mois, ou AAAA pour une annee complete)")


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _is_blank(value) -> bool:
    return value is None or (not isinstance(value, str) and pd.isna(value)) or not str(value).strip()


def month_number(value) -> int:
    """Numero de mois (1-12) a partir de 3, "03", 3.0 (Excel), "March", "mars", "Mar"..."""
    text = _strip_accents(str(value).strip().lower()).rstrip(".")
    if text.endswith(".0"):
        text = text[:-2]
    number = int(text) if text.isdigit() else MONTH_NAMES.get(text)
    if not number or not 1 <= number <= 12:
        raise ValueError(f"Mois invalide : {value!r} (attendu 1-12 ou un nom de mois, ex: 3, 03, March, mars)")
    return number


def month_range(start, end=None) -> list[dt.date]:
    """Tous les mois de start a end inclus. start/end : AAAA-MM, ou AAAA (start = janvier, end =
    decembre de l'annee). end absent : jusqu'au mois en cours."""
    first = parse_period(start)[0]
    last = parse_period(end)[-1] if not _is_blank(end) else dt.date.today().replace(day=1)
    if first > last:
        raise ValueError(f"Periode invalide : debut {first:%Y-%m} apres la fin {last:%Y-%m}")
    return [p.to_timestamp().date() for p in pd.period_range(first, last, freq="M")]


def build_selection(
    datasets: list[str], periods: list[str], direction: str | None = None,
    start: str | None = None, end: str | None = None,
) -> dict:
    """Selection saisie a la main : chaque dataset -> ensemble de (mois, sens) demandes, ou None
    (tout l'historique) si aucune periode n'est donnee. Mois = union de `periods` (--month/--year)
    et de l'intervalle start -> end (--from/--to)."""
    if end and not start:
        raise ValueError("--to sans --from : indique aussi le mois de debut (--from AAAA-MM).")
    months = {m for period in periods for m in parse_period(period)}
    if start:
        months.update(month_range(start, end))
    if not months:
        return {ds: None for ds in datasets}
    directions = [direction.title()] if direction else list(COMPARED_FLOWS)
    return {ds: {(m, d) for m in months for d in directions} for ds in datasets}


def _row_months(row) -> list[dt.date] | None:
    """Mois d'une ligne du fichier de selection, None = tout l'historique. Par ordre de priorite :
      From / To  -> intervalle (To vide = jusqu'au mois en cours) ;
      Year + Month -> ce mois (Month = 3, 03, March, mars...) ;
      Year seul  -> l'annee complete ;
      Month seul -> AAAA-MM, AAAA ou date Excel ;
      rien       -> tout l'historique."""
    year, month, start, end = (row.get(c) for c in ("Year", "Month", "From", "To"))
    if not _is_blank(start) or not _is_blank(end):
        if _is_blank(start):
            raise ValueError("To renseigne sans From : indique aussi le mois de debut.")
        return month_range(start, end)
    if not _is_blank(year):
        year_text = str(year).strip().removesuffix(".0")
        if not re.fullmatch(r"\d{4}", year_text):
            raise ValueError(f"Annee invalide : {year!r} (attendu AAAA)")
        if _is_blank(month):
            return parse_period(year_text)
        return [dt.date(int(year_text), month_number(month), 1)]
    if not _is_blank(month):
        text = str(month).strip()
        if not isinstance(month, (dt.date, dt.datetime, pd.Timestamp)) and re.fullmatch(r"\d{1,2}(\.0)?|[^\d]+", text):
            raise ValueError(f"Mois {month!r} sans annee : remplis la colonne Year (ou ecris AAAA-MM).")
        return parse_period(month)
    return None


def load_selection_file(path: str) -> dict:
    """Selection lue dans un fichier Excel ou CSV, une ligne par demande. Colonnes (casse ignoree) :
        Dataset          obligatoire (aussi : Dataset code, DataSource, Code)
        Year, Month      Year = AAAA ; Month = 3, 03, March, mars... ; Year seul = annee complete
                         (Month peut aussi etre ecrit AAAA-MM, sans colonne Year)
        From, To         intervalle de mois AAAA-MM (ou AAAA), au lieu de Year/Month ; To vide = jusqu'a aujourd'hui
        Trade Direction  Export ou Import ; vide = les deux
    Ligne sans periode = tout l'historique. Plusieurs lignes d'un meme dataset s'additionnent.
    Renvoie dataset -> set de (mois, sens), ou None pour tout l'historique."""
    df = pd.read_csv(path, dtype=str) if path.lower().endswith(".csv") else pd.read_excel(path)
    df = df.rename(columns={c: SELECTION_COLUMNS.get(_strip_accents(str(c).strip().lower()), c) for c in df.columns})
    if "Dataset" not in df.columns:
        raise SystemExit(f"{path} : colonne 'Dataset' manquante (colonnes possibles : Dataset, Year, Month, From, To, Trade Direction)")
    selection: dict = {}
    for line, row in df.iterrows():
        if _is_blank(row["Dataset"]):
            continue
        dataset = str(row["Dataset"]).strip()
        where = f"{path}, ligne {line + 2} ({dataset})"
        try:
            months = _row_months(row)
        except ValueError as exc:
            raise SystemExit(f"{where} : {exc}")
        if months is None:
            selection[dataset] = None  # tout l'historique, prime sur les mois deja demandes
            continue
        if dataset in selection and selection[dataset] is None:
            continue
        direction = row.get("Trade Direction")
        directions = [str(direction).strip().title()] if not _is_blank(direction) else list(COMPARED_FLOWS)
        selection.setdefault(dataset, set()).update((m, d) for m in months for d in directions)
    if not selection:
        raise SystemExit(f"{path} : aucun dataset trouve.")
    return selection


def _period_label(wanted: set | None) -> str:
    """Libelle court de la periode demandee, pour le resume."""
    if wanted is None:
        return "tout l'historique"
    months = sorted({m for m, _ in wanted})
    text = ", ".join(m.strftime("%Y-%m") for m in months) if len(months) <= 6 else (
        f"{months[0]:%Y-%m} -> {months[-1]:%Y-%m} ({len(months)} mois)")
    directions = sorted({d for _, d in wanted})
    return text if set(directions) == set(COMPARED_FLOWS) else f"{text} ({', '.join(directions)})"


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


def _export_import_only(raw: pd.DataFrame) -> pd.DataFrame:
    """Lignes Export/Import seulement (DEXP/REEX ramenes a Export), devise normalisee en majuscules."""
    df = raw.copy()
    df["Trade Direction"] = df["Trade Direction"].replace(TRADE_DIRECTION_ALIASES)
    df["Currency"] = df["Currency"].fillna("").astype(str).str.strip().str.upper()
    return df[df["Trade Direction"].isin(COMPARED_FLOWS)]


def currency_inventory(raw: pd.DataFrame) -> dict[str, int]:
    """Devises du dataset sur tout son historique -> nombre de (mois, sens) Export/Import ou elles
    portent une valeur non nulle, de la plus frequente a la moins frequente."""
    df = _export_import_only(raw)
    df = df[df["Total Value"].fillna(0) != 0]
    if df.empty:  # ex: aucune donnee sur la periode demandee (groupby.apply rendrait un DataFrame vide)
        return {}
    counts = df.groupby("Currency")[["Month", "Trade Direction"]].apply(lambda g: len(g.drop_duplicates()))
    return {str(c): int(n) for c, n in counts.sort_values(ascending=False).items()}


def build_monthly(raw: pd.DataFrame, te_currency: str | None) -> pd.DataFrame:
    """Totaux mensuels du dataset dans la devise de comparaison, un mois/sens par ligne.

    Si toutes les valeurs non nulles d'un mois sont dans la devise de Trading Economics
    (te_currency), le total reste dans cette devise (compared_in = te_currency, aucune conversion).
    Sinon (autre devise, plusieurs devises, devise manquante), le mois est converti en USD avec
    to_usd_monthly (compared_in = "USD"). Colonnes : Month, Trade Direction, Total Value,
    compared_in, dataset_currencies (devises non nulles du mois), conversion_errors.
    """
    keys = ["Month", "Trade Direction"]
    columns = keys + ["Total Value", "compared_in", "dataset_currencies", "conversion_errors"]
    df = _export_import_only(raw)
    if df.empty:
        return pd.DataFrame(columns=columns)

    nonzero = df[df["Total Value"].fillna(0) != 0]
    month_currencies = (
        nonzero.groupby(keys)["Currency"].agg(lambda s: ", ".join(sorted(set(s)))).rename("dataset_currencies").reset_index()
    )
    months = df[keys].drop_duplicates().merge(month_currencies, on=keys, how="left")
    months["dataset_currencies"] = months["dataset_currencies"].fillna("")

    # Mois tout a zero (aucune devise non nulle) : 0 dans n'importe quelle devise, compare tel quel.
    native = months["dataset_currencies"].isin([te_currency, ""]) if te_currency else pd.Series(False, index=months.index)
    parts = []
    native_keys = months.loc[native, keys]
    if len(native_keys):
        rows = df.merge(native_keys, on=keys)
        totals = rows.groupby(keys, as_index=False)["Total Value"].sum(min_count=1)
        parts.append(totals.assign(compared_in=te_currency, conversion_errors=""))
    usd_keys = months.loc[~native, keys]
    if len(usd_keys):
        parts.append(to_usd_monthly(df.merge(usd_keys, on=keys)).assign(compared_in="USD"))

    monthly = pd.concat(parts, ignore_index=True).merge(months, on=keys, how="left")
    return monthly[columns].sort_values(keys, ignore_index=True)


def te_in_compared_currency(te_df: pd.DataFrame, monthly: pd.DataFrame, te_currency: str | None) -> pd.DataFrame:
    """Serie TE alignee sur la devise de comparaison de chaque mois : valeur d'origine (devise TE)
    pour les mois compares en devise locale, valeur USD pour les autres. La colonne garde le nom
    value_usd attendu par _compare_country."""
    te = te_df.copy()
    if not te_currency or te_currency == "USD":
        return te
    native = monthly.loc[monthly["compared_in"] == te_currency, ["Month", "Trade Direction"]]
    native_keys = set(zip(native["Month"], native["Trade Direction"]))
    mask = [(m, str(d).strip().title()) in native_keys for m, d in zip(te["Month"], te["Direction"])]
    te.loc[mask, "value_usd"] = te.loc[mask, "value_original"]
    return te


def dataset_notes(inventory: dict[str, int], te_currency: str | None, result: pd.DataFrame) -> list[str]:
    """Points a verifier sur un dataset, en clair (colonne notes du resume)."""
    notes = []
    real = {c: n for c, n in inventory.items() if c not in MISSING_CURRENCIES}
    missing = sum(n for c, n in inventory.items() if c in MISSING_CURRENCIES)
    if len(real) > 1:
        notes.append("plusieurs devises : " + ", ".join(f"{c} ({n} mois/sens)" for c, n in real.items()))
    if missing:
        notes.append(f"devise manquante (N/A) avec valeur non nulle : {missing} mois/sens")
    if te_currency and real and te_currency not in real:
        notes.append(f"aucune valeur en {te_currency} (devise de Trading Economics) : compare apres conversion en USD")
    unconverted = result[result["conversion_errors"].fillna("") != ""]
    if len(unconverted):
        notes.append(f"conversion USD impossible : {len(unconverted)} mois/sens (NON_COMPARABLE)")
    aberrant = result[result["flag"] != ""]
    if len(aberrant):
        months = ", ".join(f"{str(m)[:7]} {d[:3]}" for m, d in zip(aberrant["Month"], aberrant["Trade Direction"]))
        notes.append(f"{len(aberrant)} mois/sens avec ecart aberrant (ratio TE/dataset > {ABERRANT_RATIO:g}x ou < 1/{ABERRANT_RATIO:g}) : {months}")
    return notes


def compare_dataset(
    dataset: str, refresh: bool, country: str | None = None, wanted: set | None = None
) -> tuple[pd.DataFrame | None, dict]:
    """Compare un dataset a Trading Economics. Renvoie (detail mensuel ou None, ligne de resume).
    `country` (ISO3) force le pays au lieu de le deduire de Clustered ou du nom du dataset.
    `wanted` : ensemble de (mois, sens) a comparer (voir build_selection), None = tout l'historique."""
    base = {"Dataset": dataset, "period": _period_label(wanted)}
    codes = [country.strip().upper()] if country else _dataset_country(dataset)
    if len(codes) != 1:
        note = "pays introuvable (Clustered et nom du dataset), utiliser --country" if not codes else f"dataset multi-pays : {codes}"
        return None, {**base, "status": "pays_indetermine", "notes": note}

    code = codes[0]
    slug, reason = resolve_te_slug(code)
    base.update({"Country": code, "TE_Slug": slug or ""})
    if slug is None:
        return None, {**base, "status": "non_mappe", "notes": reason}

    te_df, _ = _load_or_scrape_te(slug, refresh)
    if te_df.empty:
        return None, {**base, "status": "te_vide", "notes": "aucune serie Export/Import lue sur Trading Economics"}
    if not _is_te_monthly(te_df):
        return None, {**base, "status": "te_non_mensuel", "notes": "serie Trading Economics trimestrielle ou annuelle"}
    te_currencies = sorted(te_df["currency"].dropna().astype(str).str.upper().unique())
    te_currency = te_currencies[0] if len(te_currencies) == 1 else None
    base["te_currency"] = ", ".join(te_currencies)

    if wanted:
        months = sorted({m for m, _ in wanted})
        end = (pd.Timestamp(months[-1]) + pd.offsets.MonthBegin(1)).date()
        raw = _fetch_dataset_totals(dataset, months[0], end)
    else:
        raw = _fetch_dataset_totals(dataset)
    if raw.empty:
        return None, {**base, "status": "aucune_donnee_periode", "notes": "aucune valeur dans le dataset sur la periode demandee"}
    raw["Month"] = pd.to_datetime(raw["Month"]).dt.date
    inventory = currency_inventory(raw)
    base["dataset_currencies"] = ", ".join(f"{c} ({n})" for c, n in inventory.items())
    te_months = pd.to_datetime(te_df["Month"]).dt.date
    raw = raw[(raw["Month"] >= te_months.min()) & (raw["Month"] <= te_months.max())]
    print(f"  {code} -> {slug} | devise TE : {base['te_currency']} | devise(s) du dataset (mois/sens) : {base['dataset_currencies']}")

    monthly = build_monthly(raw, te_currency)
    if monthly.empty:
        return None, {**base, "status": "dataset_vide", "notes": "aucun flux Export/Import sur la periode Trading Economics"}

    result = _compare_country(monthly, te_in_compared_currency(te_df, monthly, te_currency), tolerance=TOLERANCE)
    if result.empty:
        return None, {**base, "status": "aucune_periode_commune", "notes": "aucun mois couvert par les deux sources"}

    extra = monthly[["Month", "Trade Direction", "compared_in", "dataset_currencies", "conversion_errors"]]
    result = result.merge(extra, on=["Month", "Trade Direction"], how="left")
    # Ecart signe en ratio, independant de la devise : TE / dataset (1 = identique).
    ratio = result["value_te_usd"] / result["value_clustered_usd"].where(result["value_clustered_usd"] != 0)
    result["ratio_te_dataset"] = ratio.round(4)
    result["flag"] = ""
    result.loc[(ratio > ABERRANT_RATIO) | (ratio < 1 / ABERRANT_RATIO), "flag"] = "ecart_aberrant (echelle ou devise ?)"

    missing_note = ""
    if wanted:
        keys = list(zip(result["Month"], result["Trade Direction"]))
        result = result[[k in wanted for k in keys]].reset_index(drop=True)
        missing = sorted(wanted - set(keys))
        if missing:
            listed = ", ".join(f"{m:%Y-%m} {d[:3]}" for m, d in missing[:12]) + (" ..." if len(missing) > 12 else "")
            missing_note = f"{len(missing)} mois/sens demandes sans donnees (dataset et Trading Economics, ou hors periode TE) : {listed}"
        if result.empty:
            return None, {**base, "status": "aucune_donnee_periode", "notes": missing_note}

    summary = _summarize_pair(result)  # lit value_clustered_usd / value_te_usd, avant renommage
    result = result.rename(columns={"value_clustered_usd": "value_dataset", "value_te_usd": "value_te"})
    result.insert(0, "TE_Slug", slug)
    result.insert(0, "Country", code)
    result.insert(0, "Dataset", dataset)

    # Ex: "TND: 230, USD: 8" -- mois/sens compares en devise locale vs apres conversion en USD
    # (les mois absents du dataset, presents seulement chez TE, ne sont dans aucune des deux).
    compared_in = result["compared_in"].dropna().value_counts()
    usage = ", ".join(f"{c}: {n}" for c, n in compared_in.items())
    notes = dataset_notes(inventory, te_currency, result) + ([missing_note] if missing_note else [])
    return result, {**base, "status": "compare", "compared_in": usage, **summary, "notes": " | ".join(notes)}


def save_excel(result: pd.DataFrame, summary: pd.DataFrame, path: str) -> None:
    """Classeur Excel : feuille Resume (un dataset par ligne) et Detail (un mois/sens par ligne),
    filtres, en-tete fige, montants avec separateurs de milliers, largeurs ajustees."""
    from openpyxl.utils import get_column_letter

    number_formats = {
        "value_dataset": "#,##0", "value_te": "#,##0", "ratio_te_dataset": "0.000",
        "pct_diff": EXCEL_SIGNED_PERCENT_FORMAT, "median_signed_gap": EXCEL_SIGNED_PERCENT_FORMAT,
        "match_pct": "0.00%", "median_abs_gap": "0.00%", "max_abs_gap": "0.00%",
    }
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, df in (("Resume", summary), ("Detail", result)):
            df.to_excel(writer, index=False, sheet_name=name)
            ws = writer.sheets[name]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for i, column in enumerate(df.columns, start=1):
                letter = get_column_letter(i)
                width = max([len(str(column))] + [len(str(v)) for v in df[column].head(500) if pd.notna(v)])
                ws.column_dimensions[letter].width = min(width + 2, 80)
                if column in number_formats:
                    ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width, 16)
                    for cell in ws[letter][1:]:
                        cell.number_format = number_formats[column]


def main() -> None:
    parser = argparse.ArgumentParser(description="Dataset source vs Trading Economics (en devise locale si possible, sinon en USD).")
    parser.add_argument("datasets", nargs="*", help="Datasets a comparer, ou 'all' (defaut : datasets_config.yaml).")
    parser.add_argument("--refresh", action="store_true", help="Ignore le cache TE et re-scrape.")
    parser.add_argument("--country", help="Code ISO3 du pays, si Clustered et le nom du dataset ne le donnent pas.")
    parser.add_argument("--month", nargs="+", default=[], metavar="AAAA-MM", help="Mois a comparer (un ou plusieurs).")
    parser.add_argument("--year", nargs="+", default=[], metavar="AAAA", help="Annee(s) complete(s) a comparer.")
    parser.add_argument("--from", dest="start", metavar="AAAA-MM", help="Premier mois de l'intervalle (AAAA = janvier de l'annee).")
    parser.add_argument("--to", dest="end", metavar="AAAA-MM", help="Dernier mois de l'intervalle (AAAA = decembre ; defaut : mois en cours).")
    parser.add_argument("--direction", type=str.title, choices=list(COMPARED_FLOWS), help="Export ou Import seulement (defaut : les deux).")
    parser.add_argument(
        "--file",
        help="Fichier Excel/CSV de demandes, colonnes Dataset, Month (AAAA-MM, AAAA = annee complete, "
        "vide = tout l'historique) et Trade Direction (optionnelle). Remplace datasets/--month/--year.",
    )
    parser.add_argument(
        "--output", default="dataset_vs_tradingeconomics",
        help="Debut du nom des fichiers de sortie (defaut : dataset_vs_tradingeconomics -> ..._result.xlsx).",
    )
    args = parser.parse_args()

    if args.file:
        if args.datasets or args.month or args.year or args.start or args.end or args.direction:
            raise SystemExit("--file ne se combine pas avec des datasets, --month, --year, --from/--to ou --direction : tout est dans le fichier.")
        selection = load_selection_file(args.file)
        print(f"{len(selection)} dataset(s) lu(s) dans {args.file}")
    else:
        datasets = _resolve_datasets(args.datasets)
        if datasets == "all":
            # Les series TE viennent du cache te_cache/ : seuls les pays absents du cache sont scrapes.
            datasets = [ds for ds in _list_all_datasets() if ds not in EXCLUDED_DATASOURCES]
            print(f"{len(datasets)} dataset(s) de Clustered (hors {', '.join(EXCLUDED_DATASOURCES)})")
        try:
            selection = build_selection(datasets, args.month + args.year, args.direction, args.start, args.end)
        except ValueError as exc:
            raise SystemExit(str(exc))

    output_file, summary_file, excel_file = (f"{args.output}_{suffix}" for suffix in ("result.csv", "summary.csv", "result.xlsx"))
    all_results, summary_rows = [], []
    for dataset, wanted in selection.items():
        print(f"\n########## {dataset} -- {_period_label(wanted)} ##########")
        try:
            result, summary = compare_dataset(dataset, args.refresh, args.country, wanted)
        except Exception as exc:
            error = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
            print(f"[ERREUR] {dataset} : {error}")
            summary_rows.append({"Dataset": dataset, "status": "erreur", "notes": error})
            continue
        summary_rows.append(summary)
        if result is not None:
            all_results.append(result)
            counts = result["status"].value_counts().to_dict()
            print(f"  {len(result)} mois/sens compares ({summary['compared_in']}) : {counts}, "
                  f"ecart median TE vs dataset {percent_text(summary['median_signed_gap'])}")
            if summary["notes"]:
                print("  A verifier : " + summary["notes"].replace(" | ", "\n                "))
        else:
            print(f"  {summary['status']} : {summary['notes']}")

    summary_df = pd.DataFrame(summary_rows)
    result_df = pd.concat(all_results, ignore_index=True) if all_results else pd.DataFrame()
    with_percent_text(summary_df).to_csv(summary_file, index=False)
    print(f"\nResume exporte dans {summary_file}")
    if all_results:
        with_percent_text(result_df).to_csv(output_file, index=False)
        print(f"Detail mensuel exporte dans {output_file}")
    save_excel(result_df, summary_df, excel_file)
    print(f"Classeur Excel (feuilles Resume et Detail) : {excel_file}")


if __name__ == "__main__":
    main()
