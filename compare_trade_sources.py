"""Comparaison de deux sources de donnees de trade international (Snowflake vs Clustered, reference USD)."""

from __future__ import annotations

import ast
import json
from functools import lru_cache

import pandas as pd
import pycountry

from exchange_rate import ExchangeRateError, get_monthly_average_rate

# Colonnes JSON imbriquees dans le fichier Clustered -> colonnes plates extraites (champ "Code")
CLUSTERED_JSON_COLUMNS = {
    "Country Reporter": "Country_Reporter_Code",
    "Country Partner": "Country_Partner_Code",
    "Trade Direction": "Trade_Direction_Code",
    "Currency": "Currency_Code",
}

# Colonnes de base de la jointure (Snowflake / Clustered), dans l'ordre.
SNOWFLAKE_KEY_COLUMNS = ["Reporter", "Partner", "Export", "_yearmonth_key", "_hscode_key"]
CLUSTERED_KEY_COLUMNS = [
    "Country_Reporter_Code",
    "Country_Partner_Code",
    "Trade_Direction_Code",
    "_yearmonth_key",
    "_hscode_key",
]

# Colonnes candidates pour differencier des doublons sur la cle de base, testees dans cet
# ordre de priorite. Chaque paire (colonne_snowflake, colonne_clustered) n'est utilisee que si
# les deux colonnes existent reellement dans les deux fichiers -- a adapter/completer une fois
# les vraies donnees Clustered disponibles (ex: renommer "TransportMode" si le vrai nom differe).
DIFFERENTIATOR_CANDIDATES = [
    ("Port", "Port"),
    ("TransportMode", "TransportMode"),
    ("ConsignmentCountry", "ConsignmentCountry"),
]

_KEY_SEP = "\x1f"

# Alias manuels pour les noms de pays courants que pycountry.countries.search_fuzzy() ne
# resout pas, ou resout mal. Cles en MAJUSCULES (matching insensible a la casse), consultees
# EN PREMIER par normalize_to_iso3, avant le code ISO3 direct et avant search_fuzzy.
# A completer a la main si un nouvel alias non reconnu apparait dans les vraies donnees.
COUNTRY_ALIASES = {
    "IVORY COAST": "CIV",
    "BURMA": "MMR",
    "TURKEY": "TUR",
    "DEMOCRATIC REPUBLIC OF THE CONGO": "COD",
    "DR CONGO": "COD",
    "CONGO-KINSHASA": "COD",
    "CONGO-BRAZZAVILLE": "COG",
    "SWAZILAND": "SWZ",
    "CAPE VERDE": "CPV",
    "EAST TIMOR": "TLS",
    "MACAU": "MAC",
    # search_fuzzy resout mal ces deux-la : il matche vers la Coree du Nord "PRK" a cause du
    # chevauchement de mots avec son nom officiel ("... Democratic People's Republic of
    # Korea"). Alias explicites pour eviter une mauvaise classification silencieuse.
    "REPUBLIC OF KOREA": "KOR",
    "KOREA": "KOR",
}


def _extract_json_code(cell) -> str | None:
    """Parse une cellule JSON imbriquee (ex: '{"Code": "CAN", ...}') et retourne "Code"."""
    if pd.isna(cell):
        return None
    parsed = None
    try:
        parsed = json.loads(cell)
    except (TypeError, ValueError, json.JSONDecodeError):
        try:
            parsed = ast.literal_eval(cell)
        except (TypeError, ValueError, SyntaxError):
            return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get("Code")


@lru_cache(maxsize=None)
def normalize_to_iso3(value) -> str | None:
    """Normalise un nom de pays ou un code ISO en code ISO3, via pycountry.

    Ordre de resolution : COUNTRY_ALIASES (table manuelle) -> code ISO3 direct -> recherche
    floue pycountry. Retourne None si rien ne correspond. Le resultat est mis en cache par
    valeur unique (pas d'appel pycountry redondant).
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None

    candidate = text.upper()

    if candidate in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[candidate]

    if len(candidate) == 3 and candidate.isalpha():
        match = pycountry.countries.get(alpha_3=candidate)
        if match is not None:
            return match.alpha_3

    try:
        results = pycountry.countries.search_fuzzy(text)
    except LookupError:
        return None
    return results[0].alpha_3 if results else None


def _normalize_country_columns(df: pd.DataFrame, columns: list[str], label: str) -> pd.DataFrame:
    """Normalise les colonnes pays en ISO3 (en place) et logue les valeurs non reconnues."""
    for col in columns:
        original = df[col]
        normalized = original.apply(normalize_to_iso3)

        had_value = original.notna() & (original.astype(str).str.strip() != "")
        failed_mask = normalized.isna() & had_value
        if failed_mask.any():
            counts = original[failed_mask].astype(str).str.strip().value_counts().to_dict()
            print(f"[{label}] Pays non reconnus dans '{col}' : {counts}")

        df[col] = normalized
    return df


def load_clustered(filepath: str) -> pd.DataFrame:
    """Charge le CSV Clustered (toujours en USD) et extrait les champs JSON imbriques en colonnes plates.

    Ajoute Country_Reporter_Code, Country_Partner_Code, Trade_Direction_Code
    et Currency_Code (extraits du champ "Code" de chaque cellule JSON).
    """
    df = pd.read_csv(filepath, dtype={"HS Code": str, "Date Reporter": str})

    parse_failures = {}
    for raw_col, flat_col in CLUSTERED_JSON_COLUMNS.items():
        if raw_col not in df.columns:
            raise ValueError(f"Colonne attendue absente du fichier Clustered : '{raw_col}'")
        df[flat_col] = df[raw_col].apply(_extract_json_code)
        n_failed = int(df[flat_col].isna().sum())
        if n_failed:
            parse_failures[flat_col] = n_failed

    if parse_failures:
        print(f"[load_clustered] Avertissement, cellules JSON non parsees : {parse_failures}")

    df["HS Code"] = df["HS Code"].str.strip()
    df["Date Reporter"] = df["Date Reporter"].str.strip()
    for flat_col in CLUSTERED_JSON_COLUMNS.values():
        df[flat_col] = df[flat_col].str.strip()

    df = _normalize_country_columns(df, ["Country_Reporter_Code", "Country_Partner_Code"], "load_clustered")

    return df


def _apply_value_unit_multiplier(df: pd.DataFrame) -> pd.DataFrame:
    """Calcule value_snowflake_adjusted = Value * "Value Unit multiplier".

    Multiplicateur manquant/NaN -> 1 (silencieux). Multiplicateur non numerique (texte
    inattendu) -> 1 egalement, mais avec un avertissement logue (nombre de lignes concernees).
    Si la colonne "Value Unit multiplier" n'existe pas du tout dans le fichier, equivaut a un
    multiplicateur de 1 partout, sans avertissement.
    """
    if "Value Unit multiplier" not in df.columns:
        df["value_snowflake_adjusted"] = df["Value"]
        return df

    raw = df["Value Unit multiplier"]
    had_value = raw.notna() & (raw.astype(str).str.strip() != "")
    numeric = pd.to_numeric(raw, errors="coerce")

    invalid_mask = had_value & numeric.isna()
    if invalid_mask.any():
        print(
            f"[load_snowflake] Avertissement, 'Value Unit multiplier' non numerique sur "
            f"{int(invalid_mask.sum())} lignes (traite comme multiplicateur=1)"
        )

    multiplier = numeric.fillna(1.0)
    df["value_snowflake_adjusted"] = df["Value"] * multiplier
    return df


def load_snowflake(filepath: str) -> pd.DataFrame:
    """Charge le CSV Snowflake tel quel (typage forcé en str sur les colonnes clés)."""
    df = pd.read_csv(filepath, dtype={"HSCode": str, "YearMonth": str})
    df["HSCode"] = df["HSCode"].str.strip()
    df["YearMonth"] = df["YearMonth"].str.strip()
    df["Reporter"] = df["Reporter"].str.strip()
    df["Partner"] = df["Partner"].str.strip()
    df["Export"] = df["Export"].str.strip()
    df = _normalize_country_columns(df, ["Reporter", "Partner"], "load_snowflake")
    df = _apply_value_unit_multiplier(df)
    return df


def _resolve_usd_value(value, currency, year: int, month: int) -> tuple[float | None, str]:
    """Convertit `value` en USD selon `currency`. Retourne (valeur_usd_ou_None, reason)."""
    if pd.isna(value):
        return None, "valeur_manquante"
    if pd.isna(currency):
        return None, "devise_manquante"

    currency_clean = str(currency).upper().strip()
    if currency_clean == "USD":
        return float(value), ""

    try:
        rate_info = get_monthly_average_rate(currency_clean, year, month)
    except ExchangeRateError as exc:
        return None, f"conversion_echouee: {exc}"

    return float(value) * rate_info["rate"], ""


def _build_base_key(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    """Concatene les colonnes cles en une seule chaine (cle composite de groupage)."""
    return df[columns].astype(str).agg(_KEY_SEP.join, axis=1)


def _find_duplicate_groups(snowflake: pd.DataFrame, clustered: pd.DataFrame) -> list[str]:
    """Retourne les valeurs de _base_key qui presentent une ambiguite (fan-out) a resoudre."""
    snowflake_counts = snowflake.groupby("_base_key").size()
    clustered_counts = clustered.groupby("_base_key").size()

    needs_resolution = []
    for key, sf_count in snowflake_counts.items():
        cl_count = int(clustered_counts.get(key, 0))
        if cl_count >= 1 and (sf_count > 1 or cl_count > 1):
            needs_resolution.append(key)
    return needs_resolution


def _resolve_differentiators(
    needs_resolution: list[str],
    snowflake_groups: dict[str, pd.DataFrame],
    clustered_groups: dict[str, pd.DataFrame],
) -> tuple[dict[str, tuple[str, str]], set[str], dict[str, int]]:
    """Pour chaque groupe ambigu, cherche une colonne qui le rend unique des deux cotes.

    Retourne (resolved: {key: (col_snowflake, col_clustered)}, ambiguous: {key, ...}, counts par colonne).
    """
    resolved: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    resolved_counter: dict[str, int] = {}

    for key in needs_resolution:
        sf_sub = snowflake_groups[key]
        cl_sub = clustered_groups[key]
        chosen = None

        for snowflake_col, clustered_col in DIFFERENTIATOR_CANDIDATES:
            if snowflake_col not in sf_sub.columns or clustered_col not in cl_sub.columns:
                continue
            sf_vals = sf_sub[snowflake_col]
            cl_vals = cl_sub[clustered_col]
            if sf_vals.isna().any() or cl_vals.isna().any():
                continue
            if sf_vals.nunique() != len(sf_sub) or cl_vals.nunique() != len(cl_sub):
                continue
            chosen = (snowflake_col, clustered_col)
            break

        if chosen:
            resolved[key] = chosen
            resolved_counter[chosen[0]] = resolved_counter.get(chosen[0], 0) + 1
        else:
            ambiguous.add(key)

    return resolved, ambiguous, resolved_counter


def compare_sources(df_snowflake: pd.DataFrame, df_clustered: pd.DataFrame, tolerance: float = 0.03) -> pd.DataFrame:
    """Joint les deux sources, convertit en USD, compare, et retourne le dataframe resultat.

    Jointure sur : Reporter/Country_Reporter_Code, Partner/Country_Partner_Code,
    Export/Trade_Direction_Code, YearMonth/Date Reporter[:7], HSCode/HS Code (match exact).

    Les groupes ambigus (meme cle de base repetee sans colonne pour les differencier) sont
    exclus du merge normal et marques NON_COMPARABLE / "doublon_ambigu". Les lignes Snowflake
    dont le Reporter ou le Partner n'a pas pu etre normalise en ISO3 (cf. normalize_to_iso3)
    sont exclues du merge et marquees NON_COMPARABLE / "pays_non_reconnu". Les lignes Snowflake
    sans aucune correspondance dans la source Clustered ne figurent pas dans le CSV de sortie
    mais sont comptees dans le resume affiche en fin d'execution.
    """
    snowflake_all = df_snowflake.copy()
    clustered = df_clustered.copy()

    total_snowflake_rows = len(snowflake_all)

    unresolved_country_mask = snowflake_all["Reporter"].isna() | snowflake_all["Partner"].isna()
    unresolved_country_snowflake = snowflake_all[unresolved_country_mask].copy()
    snowflake = snowflake_all[~unresolved_country_mask].copy()

    snowflake["_yearmonth_key"] = snowflake["YearMonth"]
    snowflake["_hscode_key"] = snowflake["HSCode"]
    clustered["_yearmonth_key"] = clustered["Date Reporter"].str[:7]
    clustered["_hscode_key"] = clustered["HS Code"]

    snowflake["_base_key"] = _build_base_key(snowflake, SNOWFLAKE_KEY_COLUMNS)
    clustered["_base_key"] = _build_base_key(clustered, CLUSTERED_KEY_COLUMNS)

    needs_resolution = _find_duplicate_groups(snowflake, clustered)

    snowflake_groups = dict(tuple(snowflake[snowflake["_base_key"].isin(needs_resolution)].groupby("_base_key")))
    clustered_groups = dict(tuple(clustered[clustered["_base_key"].isin(needs_resolution)].groupby("_base_key")))

    resolved_keys, ambiguous_keys, resolved_counter = _resolve_differentiators(
        needs_resolution, snowflake_groups, clustered_groups
    )

    simple_snowflake = snowflake[~snowflake["_base_key"].isin(needs_resolution)]
    ambiguous_snowflake = snowflake[snowflake["_base_key"].isin(ambiguous_keys)]

    # --- Merge "simple" : cles sans ambiguite ---
    simple_merged = pd.merge(
        simple_snowflake,
        clustered,
        left_on=SNOWFLAKE_KEY_COLUMNS,
        right_on=CLUSTERED_KEY_COLUMNS,
        how="left",
        suffixes=("_snowflake", "_clustered"),
        indicator=True,
    )

    # --- Merges "resolus" : un petit merge par groupe, avec la cle etendue du differenciateur ---
    resolved_parts = []
    for key, (snowflake_col, clustered_col) in resolved_keys.items():
        sf_sub = snowflake_groups[key]
        cl_sub = clustered_groups[key]
        part = pd.merge(
            sf_sub,
            cl_sub,
            left_on=SNOWFLAKE_KEY_COLUMNS + [snowflake_col],
            right_on=CLUSTERED_KEY_COLUMNS + [clustered_col],
            how="left",
            suffixes=("_snowflake", "_clustered"),
            indicator=True,
        )
        resolved_parts.append(part)

    merge_parts = [simple_merged] + resolved_parts
    merged_all = pd.concat(merge_parts, ignore_index=True)

    unmatched_count = int((merged_all["_merge"] == "left_only").sum())
    matched = merged_all[merged_all["_merge"] == "both"].drop(columns=["_merge"]).copy()

    matched["_year"] = matched["_yearmonth_key"].str[:4].astype(int)
    matched["_month"] = matched["_yearmonth_key"].str[5:7].astype(int)

    value_snowflake_usd, reason_snowflake = [], []
    value_clustered_usd, reason_clustered = [], []
    for _, row in matched.iterrows():
        v_snowflake, r_snowflake = _resolve_usd_value(
            row["value_snowflake_adjusted"], row["Currency_snowflake"], row["_year"], row["_month"]
        )
        v_clustered, r_clustered = _resolve_usd_value(
            row["Value_clustered"], row["Currency_Code"], row["_year"], row["_month"]
        )
        value_snowflake_usd.append(v_snowflake)
        reason_snowflake.append(r_snowflake)
        value_clustered_usd.append(v_clustered)
        reason_clustered.append(r_clustered)

    matched["value_snowflake_usd"] = value_snowflake_usd
    matched["value_clustered_usd"] = value_clustered_usd

    pct_diffs, statuses, reasons = [], [], []
    for r_snowflake, r_clustered, v_snowflake_usd, v_clustered_usd in zip(
        reason_snowflake, reason_clustered, value_snowflake_usd, value_clustered_usd
    ):
        if r_snowflake:
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append(r_snowflake)
        elif r_clustered:
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append(r_clustered)
        elif v_clustered_usd == 0:
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append("clustered_value_zero")
        else:
            pct_diff = abs(v_snowflake_usd - v_clustered_usd) / abs(v_clustered_usd)
            pct_diffs.append(pct_diff)
            statuses.append("MATCH" if pct_diff <= tolerance else "MISMATCH")
            reasons.append("")

    matched_output = pd.DataFrame(
        {
            "reporter": matched["Reporter"],
            "partner": matched["Partner"],
            "direction": matched["Export"],
            "yearmonth": matched["_yearmonth_key"],
            "hscode": matched["_hscode_key"],
            "value_snowflake_original": matched["Value_snowflake"],
            "value_snowflake_adjusted": matched["value_snowflake_adjusted"],
            "currency_snowflake": matched["Currency_snowflake"],
            "value_snowflake_usd": matched["value_snowflake_usd"],
            "value_clustered_usd": matched["value_clustered_usd"],
            "pct_diff": pct_diffs,
            "status": statuses,
            "reason": reasons,
        }
    ).reset_index(drop=True)

    # Doublons ambigus : pas de conversion/API, exclus du merge, marques directement.
    ambiguous_output = pd.DataFrame(
        {
            "reporter": ambiguous_snowflake["Reporter"],
            "partner": ambiguous_snowflake["Partner"],
            "direction": ambiguous_snowflake["Export"],
            "yearmonth": ambiguous_snowflake["_yearmonth_key"],
            "hscode": ambiguous_snowflake["_hscode_key"],
            "value_snowflake_original": ambiguous_snowflake["Value"],
            "value_snowflake_adjusted": ambiguous_snowflake["value_snowflake_adjusted"],
            "currency_snowflake": ambiguous_snowflake["Currency"],
            "value_snowflake_usd": float("nan"),
            "value_clustered_usd": float("nan"),
            "pct_diff": float("nan"),
            "status": "NON_COMPARABLE",
            "reason": "doublon_ambigu",
        }
    ).reset_index(drop=True)
    for col in ("value_snowflake_usd", "value_clustered_usd", "pct_diff"):
        ambiguous_output[col] = ambiguous_output[col].astype(float)

    # Pays non reconnu (Reporter/Partner) : pas de conversion/API, exclus du merge, marques directement.
    unresolved_country_output = pd.DataFrame(
        {
            "reporter": unresolved_country_snowflake["Reporter"],
            "partner": unresolved_country_snowflake["Partner"],
            "direction": unresolved_country_snowflake["Export"],
            "yearmonth": unresolved_country_snowflake["YearMonth"],
            "hscode": unresolved_country_snowflake["HSCode"],
            "value_snowflake_original": unresolved_country_snowflake["Value"],
            "value_snowflake_adjusted": unresolved_country_snowflake["value_snowflake_adjusted"],
            "currency_snowflake": unresolved_country_snowflake["Currency"],
            "value_snowflake_usd": float("nan"),
            "value_clustered_usd": float("nan"),
            "pct_diff": float("nan"),
            "status": "NON_COMPARABLE",
            "reason": "pays_non_reconnu",
        }
    ).reset_index(drop=True)
    for col in ("value_snowflake_usd", "value_clustered_usd", "pct_diff"):
        unresolved_country_output[col] = unresolved_country_output[col].astype(float)

    output = pd.concat([matched_output, ambiguous_output, unresolved_country_output], ignore_index=True)

    n_match = int((output["status"] == "MATCH").sum())
    n_mismatch = int((output["status"] == "MISMATCH").sum())
    n_non_comparable = int((output["status"] == "NON_COMPARABLE").sum()) + unmatched_count
    pct_mismatch = (n_mismatch / total_snowflake_rows * 100) if total_snowflake_rows else 0.0

    if resolved_counter:
        resolved_detail = ", ".join(f"{col}: {count}" for col, count in resolved_counter.items())
    else:
        resolved_detail = "aucun"

    print("\n=== Resolution des doublons ===")
    print(f"Groupes de cle de base ambigus detectes : {len(needs_resolution)}")
    print(f"  Resolus automatiquement : {len(resolved_keys)} ({resolved_detail})")
    print(f"  Restes ambigus (doublon_ambigu) : {len(ambiguous_keys)} ({len(ambiguous_snowflake)} lignes Snowflake concernees)")

    print("\n=== Normalisation des pays ===")
    print(f"Lignes Snowflake avec pays non reconnu (exclues du merge, pays_non_reconnu) : {len(unresolved_country_snowflake)}")

    print("\n=== Resume de la comparaison ===")
    print(f"Lignes comparees (total Snowflake) : {total_snowflake_rows}")
    print(f"  MATCH           : {n_match}")
    print(f"  MISMATCH        : {n_mismatch}")
    print(f"  NON_COMPARABLE  : {n_non_comparable} (dont {unmatched_count} sans correspondance trouvee)")
    print(f"  % MISMATCH      : {pct_mismatch:.2f}%")

    return output


if __name__ == "__main__":
    df_snowflake = load_snowflake("snowflake_data.csv")
    df_clustered = load_clustered("clustered_data.csv")
    result = compare_sources(df_snowflake, df_clustered, tolerance=0.03)
    result.to_csv("comparison_result.csv", index=False)
    print("\nResultat exporte dans comparison_result.csv")
