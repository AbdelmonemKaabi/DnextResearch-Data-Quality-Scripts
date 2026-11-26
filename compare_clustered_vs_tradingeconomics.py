"""Comparaison des totaux mensuels Clustered (deja en USD) avec les donnees scrapees de
Trading Economics, pays par pays et sens par sens (Export/Import).

Separe de compare_aggregated_sources.py : ici on ne compare pas Clustered a une autre source
Snowflake, mais a une source externe scrapee (te_scraper.py). Pas de detail HS ni d'unite de
quantite, seulement le total de valeur mensuel.
"""

from __future__ import annotations

import re
import time
import unicodedata

import pandas as pd
import pycountry

from snowflake_client import run_query
from te_scraper import SCRAPE_DELAY_SECONDS, scrape_country_trade_series

DATA_SOURCE = "dnexr-dnexr-all-fin-tf-customs-bilateral-monthly"
TOLERANCE = 0.03
OUTPUT_FILE = "clustered_vs_tradingeconomics_result.csv"

# Si non vide, restreint le traitement a ces codes ISO3 parmi les pays Reporter de DATA_SOURCE --
# pour tester sur un petit echantillon un DataSource multi-pays (scraping Selenium, donc lent et
# couteux en requetes). Vide = tous les pays du DataSource. Les codes doivent exister dans
# DATA_SOURCE : l'ancienne valeur ["AUS", "VNM", "DEU"] ne correspondait a aucun pays du dataset
# Finlande (seul Reporter : FIN), donc rien n'etait compare.
TEST_COUNTRY_CODES: list[str] = []

CLUSTERED_MONTHLY_QUERY = """
SELECT
    DATE_TRUNC('MONTH', "DateReporter"::DATE) AS "Month",
    COALESCE(NULLIF(TRIM(TO_VARCHAR("Reporter")), ''), 'N/A') AS "Country Reporter",
    COALESCE(NULLIF(TRIM(TO_VARCHAR("TradeFlow")), ''), 'N/A') AS "Trade Direction",
    SUM(COALESCE("Value", 0)) AS "Total Value"
FROM DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED"
WHERE "DataSource" = %(data_source)s
GROUP BY "Month", "Country Reporter", "Trade Direction"
ORDER BY "Month", "Country Reporter", "Trade Direction"
"""

COUNTRY_CODES_QUERY = """
SELECT DISTINCT "Reporter" AS "Country Code"
FROM DNEXR.FUNDAMENTALS_CUSTOMS."DNEXR-CUSTOMS-CLUSTERED"
WHERE "DataSource" = %(data_source)s
ORDER BY "Country Code"
"""

# Extrait exact du menu deroulant du site Trading Economics (nom affiche -> slug d'URL).
TE_COUNTRY_SLUGS = {
    "United States": "united-states", "United Kingdom": "united-kingdom", "Euro area": "euro-area",
    "China": "china", "Afghanistan": "afghanistan", "Albania": "albania", "Algeria": "algeria",
    "Andorra": "andorra", "Angola": "angola", "Antigua and Barbuda": "antigua-and-barbuda",
    "Argentina": "argentina", "Armenia": "armenia", "Aruba": "aruba", "Australia": "australia",
    "Austria": "austria", "Azerbaijan": "azerbaijan", "Bahamas": "bahamas", "Bahrain": "bahrain",
    "Bangladesh": "bangladesh", "Barbados": "barbados", "Belarus": "belarus", "Belgium": "belgium",
    "Belize": "belize", "Benin": "benin", "Bermuda": "bermuda", "Bhutan": "bhutan",
    "Bolivia": "bolivia", "Bosnia": "bosnia-and-herzegovina", "Botswana": "botswana",
    "Brazil": "brazil", "Brunei": "brunei", "Bulgaria": "bulgaria", "Burkina Faso": "burkina-faso",
    "Burundi": "burundi", "Cambodia": "cambodia", "Cameroon": "cameroon", "Canada": "canada",
    "Cape Verde": "cape-verde", "Cayman Islands": "cayman-islands",
    "Central African Republic": "central-african-republic", "Chad": "chad",
    "Channel Islands": "channel-islands", "Chile": "chile", "Colombia": "colombia",
    "Comoros": "comoros", "Congo": "congo", "Costa Rica": "costa-rica",
    "Cote d Ivoire": "cote-d-ivoire", "Croatia": "croatia", "Cuba": "cuba", "Cyprus": "cyprus",
    "Czech Republic": "czech-republic", "Denmark": "denmark", "Djibouti": "djibouti",
    "Dominica": "dominica", "Dominican Republic": "dominican-republic",
    "East Timor": "east-timor", "Ecuador": "ecuador", "Egypt": "egypt", "El Salvador": "el-salvador",
    "Equatorial Guinea": "equatorial-guinea", "Eritrea": "eritrea", "Estonia": "estonia",
    "Ethiopia": "ethiopia", "Faeroe Islands": "faeroe-islands", "Fiji": "fiji", "Finland": "finland",
    "France": "france", "French Polynesia": "french-polynesia", "Gabon": "gabon",
    "Gambia": "gambia", "Georgia": "georgia", "Germany": "germany", "Ghana": "ghana",
    "Greece": "greece", "Greenland": "greenland", "Grenada": "grenada", "Guam": "guam",
    "Guatemala": "guatemala", "Guinea": "guinea", "Guinea Bissau": "guinea-bissau",
    "Guyana": "guyana", "Haiti": "haiti", "Honduras": "honduras", "Hong Kong": "hong-kong",
    "Hungary": "hungary", "Iceland": "iceland", "India": "india", "Indonesia": "indonesia",
    "Iran": "iran", "Iraq": "iraq", "Ireland": "ireland", "Isle of Man": "isle-of-man",
    "Israel": "israel", "Italy": "italy", "Ivory Coast": "ivory-coast", "Jamaica": "jamaica",
    "Japan": "japan", "Jordan": "jordan", "Kazakhstan": "kazakhstan", "Kenya": "kenya",
    "Kiribati": "kiribati", "Kosovo": "kosovo", "Kuwait": "kuwait", "Kyrgyzstan": "kyrgyzstan",
    "Laos": "laos", "Latvia": "latvia", "Lebanon": "lebanon", "Lesotho": "lesotho",
    "Liberia": "liberia", "Libya": "libya", "Liechtenstein": "liechtenstein",
    "Lithuania": "lithuania", "Luxembourg": "luxembourg", "Macau": "macau",
    "Macedonia": "macedonia", "Madagascar": "madagascar", "Malawi": "malawi",
    "Malaysia": "malaysia", "Maldives": "maldives", "Mali": "mali", "Malta": "malta",
    "Marshall Islands": "marshall-islands", "Mauritania": "mauritania", "Mauritius": "mauritius",
    "Mayotte": "mayotte", "Mexico": "mexico", "Micronesia": "micronesia", "Moldova": "moldova",
    "Monaco": "monaco", "Mongolia": "mongolia", "Montenegro": "montenegro", "Morocco": "morocco",
    "Mozambique": "mozambique", "Myanmar": "myanmar", "Namibia": "namibia", "Nepal": "nepal",
    "Netherlands": "netherlands", "Netherlands Antilles": "netherlands-antilles",
    "New Caledonia": "new-caledonia", "New Zealand": "new-zealand", "Nicaragua": "nicaragua",
    "Niger": "niger", "Nigeria": "nigeria", "North Korea": "north-korea", "Norway": "norway",
    "Oman": "oman", "Pakistan": "pakistan", "Palau": "palau", "Panama": "panama",
    "Palestine": "palestine", "Papua New Guinea": "papua-new-guinea", "Paraguay": "paraguay",
    "Peru": "peru", "Philippines": "philippines", "Poland": "poland", "Portugal": "portugal",
    "Puerto Rico": "puerto-rico", "Qatar": "qatar",
    "Republic of the Congo": "republic-of-the-congo", "Romania": "romania", "Russia": "russia",
    "Rwanda": "rwanda", "Samoa": "samoa", "Sao Tome and Principe": "sao-tome-and-principe",
    "Saudi Arabia": "saudi-arabia", "Senegal": "senegal", "Serbia": "serbia",
    "Seychelles": "seychelles", "Sierra Leone": "sierra-leone", "Singapore": "singapore",
    "Slovakia": "slovakia", "Slovenia": "slovenia", "Solomon Islands": "solomon-islands",
    "Somalia": "somalia", "South Africa": "south-africa", "South Korea": "south-korea",
    "South Sudan": "south-sudan", "Spain": "spain", "Sri Lanka": "sri-lanka", "Sudan": "sudan",
    "Suriname": "suriname", "Swaziland": "swaziland", "Sweden": "sweden",
    "Switzerland": "switzerland", "Syria": "syria", "Taiwan": "taiwan", "Tajikistan": "tajikistan",
    "Tanzania": "tanzania", "Thailand": "thailand", "Timor Leste": "timor-leste", "Togo": "togo",
    "Tonga": "tonga", "Trinidad and Tobago": "trinidad-and-tobago", "Tunisia": "tunisia",
    "Turkey": "turkey", "Turkmenistan": "turkmenistan", "Uganda": "uganda", "Ukraine": "ukraine",
    "United Arab Emirates": "united-arab-emirates", "Uruguay": "uruguay",
    "Uzbekistan": "uzbekistan", "Vanuatu": "vanuatu", "Venezuela": "venezuela",
    "Vietnam": "vietnam", "Virgin Islands": "virgin-islands", "Yemen": "yemen",
    "Zambia": "zambia", "Zimbabwe": "zimbabwe",
}

# Cas ou le nom officiel pycountry (ISO 3166) diverge structurellement (mot different, forme
# longue officielle) du libelle utilise par Trading Economics -- mappe directement vers le slug
# TE. Compare via _normalize_country_name des deux cotes, donc les variantes d'accents/ponctuation
# (ex: "Cote d'Ivoire" -> "Guinea-Bissau") n'ont pas besoin d'entree ici, seulement les vrais
# synonymes/formes longues.
COUNTRY_NAME_ALIASES = {
    "Korea, Republic of": "south-korea",
    "Korea, Democratic People's Republic of": "north-korea",
    "Russian Federation": "russia",
    "Iran, Islamic Republic of": "iran",
    "Venezuela, Bolivarian Republic of": "venezuela",
    "Viet Nam": "vietnam",
    "Syrian Arab Republic": "syria",
    "Lao People's Democratic Republic": "laos",
    "Bolivia, Plurinational State of": "bolivia",
    "Moldova, Republic of": "moldova",
    "Tanzania, United Republic of": "tanzania",
    "Brunei Darussalam": "brunei",
    "Czechia": "czech-republic",
    "North Macedonia": "macedonia",
    "Cabo Verde": "cape-verde",
    "Eswatini": "swaziland",
    "Bosnia and Herzegovina": "bosnia-and-herzegovina",
    "Taiwan, Province of China": "taiwan",
    "Macao": "macau",
    "Palestine, State of": "palestine",
    "Micronesia, Federated States of": "micronesia",
    "Virgin Islands, U.S.": "virgin-islands",
    "Virgin Islands, British": "virgin-islands",
    # Trading Economics nomme la RD Congo "Congo" (et le Congo-Brazzaville "Republic of the Congo").
    "Congo, The Democratic Republic of the": "congo",
    # Nom ISO actuel (pycountry) ; Trading Economics garde "Turkey".
    "Türkiye": "turkey",
}


# Codes pays resolus directement en slug Trading Economics, avant toute recherche par nom :
#   - COG : son nom pycountry "Congo" correspond exactement a la page TE "congo", qui est en
#     realite la RD Congo (COD) ; le Congo-Brazzaville est "republic-of-the-congo" chez TE ;
#   - KOSOV : code Reporter Clustered hors ISO 3166, inconnu de pycountry.
CODE_SLUG_OVERRIDES = {"COG": "republic-of-the-congo", "KOSOV": "kosovo"}


def _normalize_country_name(name: str) -> str:
    """Normalise un nom de pays pour comparaison tolerante aux accents/ponctuation/casse.

    Retire les accents (ex: "Cote" reste "Cote" apres suppression du "^" de "Côte"), remplace
    toute ponctuation (apostrophes, virgules, tirets) par un espace, et met en minuscules. Permet
    de faire correspondre par ex. le nom pycountry "Côte d'Ivoire" au libelle TE "Cote d Ivoire"
    sans avoir besoin d'une entree explicite dans COUNTRY_NAME_ALIASES.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", ascii_name)
    return cleaned.strip().lower()


_TE_SLUGS_NORMALIZED = {_normalize_country_name(name): slug for name, slug in TE_COUNTRY_SLUGS.items()}
_ALIASES_NORMALIZED = {_normalize_country_name(name): slug for name, slug in COUNTRY_NAME_ALIASES.items()}


def resolve_te_slug(iso3_code: str) -> tuple[str | None, str]:
    """Convertit un code pays Clustered (ISO3) en slug Trading Economics.

    Ordre de resolution : (1) nom pycountry exact dans TE_COUNTRY_SLUGS, (2) nom pycountry
    normalise (accents/ponctuation ignores) contre les cles TE normalisees, (3) alias explicite
    pour les vrais synonymes/formes longues (COUNTRY_NAME_ALIASES). Si rien ne correspond, renvoie
    (None, raison) -- jamais d'echec silencieux, l'appelant doit logger `raison` et exclure le pays.
    """
    code = str(iso3_code).strip().upper()
    if not code or code == "N/A":
        return None, "code_pays_vide"
    if code in CODE_SLUG_OVERRIDES:
        return CODE_SLUG_OVERRIDES[code], ""

    country = pycountry.countries.get(alpha_3=code)
    if country is None:
        return None, f"code_iso3_inconnu_pycountry({code})"

    name = country.name
    if name in TE_COUNTRY_SLUGS:
        return TE_COUNTRY_SLUGS[name], ""

    normalized = _normalize_country_name(name)
    if normalized in _TE_SLUGS_NORMALIZED:
        return _TE_SLUGS_NORMALIZED[normalized], ""

    if normalized in _ALIASES_NORMALIZED:
        return _ALIASES_NORMALIZED[normalized], ""

    return None, f"pays_non_mappe({code}={name})"


def _fetch_clustered_totals() -> pd.DataFrame:
    """Recupere et agrege les totaux mensuels Clustered (Month, Country Reporter, Trade Direction)."""
    return run_query(CLUSTERED_MONTHLY_QUERY, params={"data_source": DATA_SOURCE})


def _fetch_country_codes() -> list[str]:
    """Renvoie les codes pays (ISO3) distincts presents dans Clustered pour DATA_SOURCE."""
    df = run_query(COUNTRY_CODES_QUERY, params={"data_source": DATA_SOURCE})
    return [c for c in df["Country Code"].dropna().astype(str).tolist() if c.strip()]


def _compare_country(clustered_sub: pd.DataFrame, te_df: pd.DataFrame, tolerance: float) -> pd.DataFrame:
    """Joint (outer) Clustered et Trading Economics sur (Month, Trade Direction) pour un pays,
    puis calcule pct_diff/status/reason par ligne.

    Regles (identiques a compare_aggregated_sources.py) : 0=0 -> MATCH ; valeur clustered a 0
    (et TE non nul) -> NON_COMPARABLE ; mois absent d'un cote -> NON_COMPARABLE
    ("sans_correspondance") ; conversion USD TE echouee (value_te_usd NaN) -> NON_COMPARABLE
    ("te_conversion_echouee") ; valeur source NaN (conversion USD du dataset echouee) ->
    NON_COMPARABLE ("source_conversion_echouee") ; sinon MATCH/MISMATCH selon le seuil `tolerance`.
    Deux points TE dans un meme mois : le dernier est garde, reason "te_mois_en_double_dernier_point_garde".
    """
    clustered = clustered_sub[["Month", "Trade Direction", "Total Value"]].copy()
    clustered["Trade Direction"] = clustered["Trade Direction"].astype(str).str.strip().str.title()
    clustered = clustered.rename(columns={"Total Value": "value_clustered_usd"})
    clustered["value_clustered_usd"] = pd.to_numeric(clustered["value_clustered_usd"], errors="raise").astype("float64")

    te = te_df.rename(columns={"Direction": "Trade Direction", "value_usd": "value_te_usd"})
    te["Trade Direction"] = te["Trade Direction"].astype(str).str.strip().str.title()
    # Le graphique TE a parfois DEUX points dans le meme mois (ex: Vietnam 2020-08 : 26.5 puis
    # 27.7 Mds, estimation provisoire puis chiffre definitif). Les additionner doublait le mois.
    # Les points arrivent dans l'ordre chronologique du graphique : on garde le dernier (le plus
    # recent), qui est celui qui correspond aux donnees officielles dans tous les cas verifies
    # (Vietnam 4/4 a l'identique, Philippines, Thailande). Le mois reste compare, mais sa colonne
    # reason vaut "te_mois_en_double_dernier_point_garde" pour qu'il reste reperable.
    duplicated = te.duplicated(["Month", "Trade Direction"], keep="last")
    duplicated_months = set(zip(te.loc[duplicated, "Month"], te.loc[duplicated, "Trade Direction"]))
    if duplicated.any():
        print(f"[INFO] {int(duplicated.sum())} point(s) Trading Economics en double dans un meme mois : dernier point garde.")
        te = te[~duplicated]
    # min_count=1 : un mois dont la conversion USD a echoue (NaN) reste NaN -> NON_COMPARABLE
    # "te_conversion_echouee", au lieu d'etre additionne a 0 (faux ecart de -100 %).
    te = te.groupby(["Month", "Trade Direction"], as_index=False)["value_te_usd"].sum(min_count=1)

    # Fenetre commune : sans abonnement, Trading Economics ne publie qu'environ 10 ans d'historique
    # (bouton 10Y) alors que Clustered peut remonter bien plus loin (1996 pour la Finlande), et
    # chaque source peut avoir un mois d'avance sur l'autre. Un mois hors de la periode couverte
    # par les DEUX sources n'est pas un ecart : il est exclu (et compte dans le log). Seul un mois
    # manquant A L'INTERIEUR de cette fenetre reste "sans_correspondance".
    if len(clustered) and len(te):
        start = max(clustered["Month"].min(), te["Month"].min())
        end = min(clustered["Month"].max(), te["Month"].max())
        in_window_cl = (clustered["Month"] >= start) & (clustered["Month"] <= end)
        in_window_te = (te["Month"] >= start) & (te["Month"] <= end)
        print(
            f"Fenetre commune : {start} -> {end} -- exclus hors fenetre : "
            f"{int((~in_window_cl).sum())} ligne(s) Clustered, {int((~in_window_te).sum())} ligne(s) Trading Economics"
        )
        clustered, te = clustered[in_window_cl], te[in_window_te]

    merged = pd.merge(clustered, te, on=["Month", "Trade Direction"], how="outer", indicator=True)

    pct_diffs, statuses, reasons = [], [], []
    for _, row in merged.iterrows():
        if row["_merge"] != "both":
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append("sans_correspondance")
        elif pd.isna(row["value_te_usd"]):
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append("te_conversion_echouee")
        elif pd.isna(row["value_clustered_usd"]):
            # Cas du dataset source converti en USD (compare_dataset_vs_tradingeconomics.py) : un
            # taux de change introuvable donne NaN, qui sinon sortirait en faux MISMATCH.
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append("source_conversion_echouee")
        elif row["value_clustered_usd"] == 0 and row["value_te_usd"] == 0:
            pct_diffs.append(0.0)
            statuses.append("MATCH")
            reasons.append("")
        elif row["value_clustered_usd"] == 0:
            pct_diffs.append(None)
            statuses.append("NON_COMPARABLE")
            reasons.append("clustered_value_zero")
        else:
            pct_diff = abs(row["value_te_usd"] - row["value_clustered_usd"]) / abs(row["value_clustered_usd"])
            pct_diffs.append(pct_diff)
            statuses.append("MATCH" if pct_diff <= tolerance else "MISMATCH")
            reasons.append("")

    merged["pct_diff"] = pct_diffs
    merged["status"] = statuses
    merged["reason"] = reasons
    # Mois TE en double (dernier point garde, voir plus haut) : compare normalement, mais signale.
    flagged = [(m, d) in duplicated_months and r == "" for m, d, r in zip(merged["Month"], merged["Trade Direction"], merged["reason"])]
    merged.loc[flagged, "reason"] = "te_mois_en_double_dernier_point_garde"

    return merged[["Month", "Trade Direction", "value_clustered_usd", "value_te_usd", "pct_diff", "status", "reason"]]


if __name__ == "__main__":
    print(f"########## Recuperation des totaux Clustered (DataSource={DATA_SOURCE}) ##########")
    df_clustered_all = _fetch_clustered_totals()
    print(f"{len(df_clustered_all)} ligne(s) agregee(s) recuperees.")

    country_codes = _fetch_country_codes()
    print(f"{len(country_codes)} code(s) pays distinct(s) trouve(s) : {country_codes}")

    if TEST_COUNTRY_CODES:
        print(f"\n[MODE TEST] TEST_COUNTRY_CODES non vide -- restriction a : {TEST_COUNTRY_CODES}")
        country_codes = [c for c in TEST_COUNTRY_CODES if c in country_codes]
        missing = [c for c in TEST_COUNTRY_CODES if c not in country_codes]
        if missing:
            print(f"[MODE TEST] Code(s) absent(s) de Clustered pour ce DataSource (ignores) : {missing}")

    mapped_countries: list[tuple[str, str]] = []
    unmapped_countries: list[tuple[str, str]] = []
    for code in country_codes:
        slug, reason = resolve_te_slug(code)
        if slug is None:
            unmapped_countries.append((code, reason))
        else:
            mapped_countries.append((code, slug))

    if unmapped_countries:
        print(f"\n[ATTENTION] {len(unmapped_countries)} code(s) pays non mappe(s) vers Trading Economics (exclus) :")
        for code, reason in unmapped_countries:
            print(f"  - {code} : {reason}")

    print(f"\n{len(mapped_countries)} pays mappe(s), scraping en cours...")

    all_results = []
    failed_countries = []
    for i, (code, slug) in enumerate(mapped_countries):
        print(f"\n########## Pays : {code} -> {slug} ##########")
        try:
            clustered_sub = df_clustered_all[df_clustered_all["Country Reporter"] == code]
            te_df = scrape_country_trade_series(slug)
            result = _compare_country(clustered_sub, te_df, tolerance=TOLERANCE)
            result.insert(0, "TE_Slug", slug)
            result.insert(0, "Country", code)
            all_results.append(result)
        except Exception as exc:
            print(f"[ERREUR] Pays '{code}' ({slug}) ignore : {exc}")
            failed_countries.append(code)

        if i < len(mapped_countries) - 1:
            time.sleep(SCRAPE_DELAY_SECONDS)

    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        combined.to_csv(OUTPUT_FILE, index=False)
        print(f"\nResultat exporte dans {OUTPUT_FILE} ({len(combined)} ligne(s), {len(all_results)} pays)")

        print("\n=== Resume ===")
        print(f"Pays traites avec succes : {len(all_results)} / {len(mapped_countries)} mappe(s)")
        print(f"Pays non mappes (exclus) : {len(unmapped_countries)}")
        print(f"Pays en echec (scraping/erreur) : {len(failed_countries)} {failed_countries}")
        for status in ("MATCH", "MISMATCH", "NON_COMPARABLE"):
            n = int((combined["status"] == status).sum())
            pct = (n / len(combined) * 100) if len(combined) else 0.0
            print(f"  {status:15s}: {n} ({pct:.2f}%)")
    else:
        print("\n[ATTENTION] Aucun resultat produit (tous les pays mappes ont echoue ou aucun pays mappe).")
        print(f"Pays non mappes (exclus) : {len(unmapped_countries)}")
        print(f"Pays en echec (scraping/erreur) : {len(failed_countries)} {failed_countries}")
