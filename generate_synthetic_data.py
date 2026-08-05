"""Genere un jeu de donnees synthetique complet (snowflake_data.csv, clustered_data.csv) pour
couvrir tous les cas geres par compare_trade_sources.py, en attendant les vraies donnees.

Toutes les valeurs sont fixees a la main (pas de random) pour que chaque ligne ait un
resultat attendu connu et verifiable dans comparison_result.csv.

NOTE : le fichier Clustered synthetique contient ici des colonnes Port/TransportMode/
ConsignmentCountry qui n'existent PAS forcement dans le vrai schema Clustered tel que
decrit. Elles sont ajoutees uniquement pour demontrer la resolution automatique de doublons
(cas 9). A retirer/adapter une fois les vraies donnees disponibles si elles n'existent pas
reellement.
"""

from __future__ import annotations

import json

import pandas as pd

SNOWFLAKE_DEFAULTS = dict(
    Reporter="CAN",
    Partner="USA",
    Export="Export",
    DataSource="SRC1",
    DateReporter="2024-06-15",
    YearMonth="2024-06",
    HSCode="00000000",
    Currency="USD",
    Unit="KG",
    Quantity=10,
    Quantity2=0,
    Value=1000.0,
    ValueUnitMultiplier=1,
    Port="P0",
    TransportMode="Air",
    Province="ON",
    State="",
    CustomsOffice="C1",
    ConsignmentCountry="USA",
    TradeDirection="Export",
    Unit2="KG",
)

CLUSTERED_DEFAULTS = dict(
    CutOffDate="2024-07-01",
    DataSource="SRC2",
    ReporterCode="CAN",
    PartnerCode="USA",
    DirectionCode="Export",
    Product="Widget",
    Quantity=10,
    QuantityUnit="KG",
    QuantityUnitMultiplier=1,
    DateReporter="2024-06-15",
    Value=1000.0,
    ValueUnitMultiplier=1,
    CurrencyCode="USD",
    HSCode="00000000",
    Quantity2=0,
    Quantity2Unit="KG",
    Quantity2UnitMultiplier=1,
    Port="P0",
    TransportMode="Air",
    ConsignmentCountry="USA",
)


def make_snowflake_row(**overrides) -> dict:
    row = dict(SNOWFLAKE_DEFAULTS)
    row["Export"] = overrides.get("Export", row["Export"])
    row.update(overrides)
    row["TradeDirection"] = row["Export"]
    return row


def make_clustered_row(**overrides) -> dict:
    row = dict(CLUSTERED_DEFAULTS)
    row.update(overrides)
    return row


def _json_code(code) -> str:
    if code is None or (isinstance(code, float) and pd.isna(code)):
        return ""
    return json.dumps({"Code": code})


def clustered_row_to_csv_dict(row: dict) -> dict:
    return {
        "Cut-Off Date": row["CutOffDate"],
        "Data Source": row["DataSource"],
        "Country Reporter": _json_code(row["ReporterCode"]),
        "Country Partner": _json_code(row["PartnerCode"]),
        "Trade Direction": _json_code(row["DirectionCode"]),
        "Product": row["Product"],
        "Quantity": row["Quantity"],
        "Quantity Unit": row["QuantityUnit"],
        "Quantity Unit multiplier": row["QuantityUnitMultiplier"],
        "Date Reporter": row["DateReporter"],
        "Value": row["Value"],
        "Value Unit multiplier": row["ValueUnitMultiplier"],
        "Currency": _json_code(row["CurrencyCode"]),
        "HS Code": row["HSCode"],
        "Quantity 2": row["Quantity2"],
        "Quantity 2 Unit": row["Quantity2Unit"],
        "Quantity 2 Unit multiplier": row["Quantity2UnitMultiplier"],
        "Port": row["Port"],
        "TransportMode": row["TransportMode"],
        "ConsignmentCountry": row["ConsignmentCountry"],
    }


SNOWFLAKE_ROWS: list[dict] = []
CLUSTERED_ROWS: list[dict] = []

# ---------------------------------------------------------------------------
# Cas 1 : MATCH (USD/USD), ecarts < 3%
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="10000001", Value=1000.0),  # vs 1020 -> 1.96%
    make_snowflake_row(HSCode="10000002", Value=2000.0),  # vs 2000 -> 0%
    make_snowflake_row(HSCode="10000003", Value=750.0),  # vs 730 -> 2.74%
    make_snowflake_row(HSCode="10000004", Value=100.0),  # vs 102 -> 1.96%
    make_snowflake_row(HSCode="10000005", Value=999.0),  # vs 985 -> 1.42%
    make_snowflake_row(HSCode="10000006", Value=1200.0),  # vs 1215 -> 1.23%
    make_snowflake_row(HSCode="10000007", Value=800.0),  # vs 815 -> 1.84%
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="10000001", Value=1020.0),
    make_clustered_row(HSCode="10000002", Value=2000.0),
    make_clustered_row(HSCode="10000003", Value=730.0),
    make_clustered_row(HSCode="10000004", Value=102.0),
    make_clustered_row(HSCode="10000005", Value=985.0),
    make_clustered_row(HSCode="10000006", Value=1215.0),
    make_clustered_row(HSCode="10000007", Value=815.0),
]

# ---------------------------------------------------------------------------
# Cas 2 : MISMATCH (USD/USD), ecarts varies : ~5%, ~15%, ~25%, ~50%
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="20000001", Value=1000.0),  # vs 950 -> 5.26%
    make_snowflake_row(HSCode="20000002", Value=2000.0),  # vs 1900 -> 5.26%
    make_snowflake_row(HSCode="20000003", Value=1000.0),  # vs 870 -> 14.9%
    make_snowflake_row(HSCode="20000004", Value=500.0),  # vs 435 -> 14.9%
    make_snowflake_row(HSCode="20000005", Value=1500.0),  # vs 1000 -> 50%
    make_snowflake_row(HSCode="20000006", Value=300.0),  # vs 200 -> 50%
    make_snowflake_row(HSCode="20000007", Value=1250.0),  # vs 1000 -> 25%
    make_snowflake_row(HSCode="20000008", Value=400.0),  # vs 320 -> 25%
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="20000001", Value=950.0),
    make_clustered_row(HSCode="20000002", Value=1900.0),
    make_clustered_row(HSCode="20000003", Value=870.0),
    make_clustered_row(HSCode="20000004", Value=435.0),
    make_clustered_row(HSCode="20000005", Value=1000.0),
    make_clustered_row(HSCode="20000006", Value=200.0),
    make_clustered_row(HSCode="20000007", Value=1000.0),
    make_clustered_row(HSCode="20000008", Value=320.0),
]

# ---------------------------------------------------------------------------
# Cas 3 : conversion USD directe (dedie, deja aussi couvert par cas 1/2)
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="30000001", Value=5000.0, Currency="USD"),  # vs 5050 -> 0.99%
    make_snowflake_row(HSCode="30000002", Value=300.0, Currency="USD"),  # vs 300 -> 0%
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="30000001", Value=5050.0, CurrencyCode="USD"),
    make_clustered_row(HSCode="30000002", Value=300.0, CurrencyCode="USD"),
]

# ---------------------------------------------------------------------------
# Cas 4 : conversion devise etrangere (EUR, TRY, JPY), mois varies, teste cache
# Taux approximatifs utilises pour la valeur Clustered -- le statut MATCH/MISMATCH reel
# depend du taux exact renvoye par Frankfurter au moment de l'execution.
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="40000001", Currency="EUR", DateReporter="2023-06-15", YearMonth="2023-06", Value=1000.0),
    make_snowflake_row(HSCode="40000002", Currency="EUR", DateReporter="2023-06-15", YearMonth="2023-06", Value=500.0),
    make_snowflake_row(HSCode="40000003", Currency="TRY", DateReporter="2024-03-15", YearMonth="2024-03", Value=100000.0),
    make_snowflake_row(HSCode="40000004", Currency="TRY", DateReporter="2024-03-15", YearMonth="2024-03", Value=50000.0),
    make_snowflake_row(HSCode="40000005", Currency="JPY", DateReporter="2023-09-15", YearMonth="2023-09", Value=1000000.0),
    make_snowflake_row(HSCode="40000006", Currency="JPY", DateReporter="2023-09-15", YearMonth="2023-09", Value=2000000.0),
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="40000001", DateReporter="2023-06-15", Value=1080.0, CurrencyCode="USD"),
    make_clustered_row(HSCode="40000002", DateReporter="2023-06-15", Value=540.0, CurrencyCode="USD"),
    make_clustered_row(HSCode="40000003", DateReporter="2024-03-15", Value=3100.0, CurrencyCode="USD"),
    make_clustered_row(HSCode="40000004", DateReporter="2024-03-15", Value=1550.0, CurrencyCode="USD"),
    make_clustered_row(HSCode="40000005", DateReporter="2023-09-15", Value=6800.0, CurrencyCode="USD"),
    make_clustered_row(HSCode="40000006", DateReporter="2023-09-15", Value=13600.0, CurrencyCode="USD"),
]

# ---------------------------------------------------------------------------
# Cas 5 : HSCode sans correspondance (aucune ligne Clustered avec ce code)
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="50000001", Value=1000.0),
    make_snowflake_row(HSCode="50000002", Value=2000.0),
    make_snowflake_row(HSCode="50000003", Value=3000.0),
]
# (volontairement aucune ligne Clustered correspondante)

# ---------------------------------------------------------------------------
# Cas 6 : Reporter/Partner/Direction sans correspondance
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="60000001", Partner="BRA", Value=1000.0),  # Partner absent cote Clustered
    make_snowflake_row(HSCode="60000002", Partner="MEX", Export="Export", Value=1000.0),  # Direction incoherente
    make_snowflake_row(HSCode="60000003", Reporter="IND", Value=1000.0),  # Reporter absent cote Clustered
]
CLUSTERED_ROWS += [
    # Pour 60000002 : meme cle sauf Direction = Import cote Clustered -> pas de match
    make_clustered_row(HSCode="60000002", PartnerCode="MEX", DirectionCode="Import", Value=1000.0),
]

# ---------------------------------------------------------------------------
# Cas 7 : value_clustered_usd == 0
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="70000001", Value=1000.0),
    make_snowflake_row(HSCode="70000002", Value=500.0),
    make_snowflake_row(HSCode="70000003", Value=250.0),
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="70000001", Value=0.0),
    make_clustered_row(HSCode="70000002", Value=0.0),
    make_clustered_row(HSCode="70000003", Value=0.0),
]

# ---------------------------------------------------------------------------
# Cas 8 : devise invalide/inconnue (echec API Frankfurter attendu)
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="80000001", Currency="XXX", Value=1000.0),
    make_snowflake_row(HSCode="80000002", Currency="XXX", Value=2000.0),
    make_snowflake_row(HSCode="80000003", Currency="ZZZ", Value=500.0),
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="80000001", Value=1000.0),
    make_clustered_row(HSCode="80000002", Value=2000.0),
    make_clustered_row(HSCode="80000003", Value=500.0),
]

# ---------------------------------------------------------------------------
# Cas 9 : doublons resolvables (une colonne differenciante suffit)
# Groupe A : resolu via Port (1ere priorite)
# Groupe B : Port identique -> resolu via TransportMode (2e priorite)
# Groupe C : Port + TransportMode identiques -> resolu via ConsignmentCountry (3e priorite)
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="90000001", Partner="USA", Export="Export", Value=100.0, Port="P1", TransportMode="Air", ConsignmentCountry="USA"),
    make_snowflake_row(HSCode="90000001", Partner="USA", Export="Export", Value=200.0, Port="P2", TransportMode="Air", ConsignmentCountry="USA"),
    make_snowflake_row(HSCode="90000002", Partner="DEU", Export="Import", Value=300.0, Port="P3", TransportMode="Air", ConsignmentCountry="DEU"),
    make_snowflake_row(HSCode="90000002", Partner="DEU", Export="Import", Value=400.0, Port="P3", TransportMode="Sea", ConsignmentCountry="DEU"),
    make_snowflake_row(HSCode="90000003", Partner="GBR", Export="Export", Value=500.0, Port="P4", TransportMode="Sea", ConsignmentCountry="USA"),
    make_snowflake_row(HSCode="90000003", Partner="GBR", Export="Export", Value=600.0, Port="P4", TransportMode="Sea", ConsignmentCountry="MEX"),
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="90000001", PartnerCode="USA", DirectionCode="Export", Value=105.0, Port="P1", TransportMode="Air", ConsignmentCountry="USA"),
    make_clustered_row(HSCode="90000001", PartnerCode="USA", DirectionCode="Export", Value=204.0, Port="P2", TransportMode="Air", ConsignmentCountry="USA"),
    make_clustered_row(HSCode="90000002", PartnerCode="DEU", DirectionCode="Import", Value=306.0, Port="P3", TransportMode="Air", ConsignmentCountry="DEU"),
    make_clustered_row(HSCode="90000002", PartnerCode="DEU", DirectionCode="Import", Value=408.0, Port="P3", TransportMode="Sea", ConsignmentCountry="DEU"),
    make_clustered_row(HSCode="90000003", PartnerCode="GBR", DirectionCode="Export", Value=510.0, Port="P4", TransportMode="Sea", ConsignmentCountry="USA"),
    make_clustered_row(HSCode="90000003", PartnerCode="GBR", DirectionCode="Export", Value=612.0, Port="P4", TransportMode="Sea", ConsignmentCountry="MEX"),
]

# ---------------------------------------------------------------------------
# Cas 10 : doublons non resolvables (Port/TransportMode/ConsignmentCountry identiques)
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="91000001", Partner="JPN", Export="Export", Value=700.0, Port="P5", TransportMode="Air", ConsignmentCountry="JPN"),
    make_snowflake_row(HSCode="91000001", Partner="JPN", Export="Export", Value=800.0, Port="P5", TransportMode="Air", ConsignmentCountry="JPN"),
    make_snowflake_row(HSCode="91000002", Partner="KOR", Export="Import", Value=900.0, Port="P6", TransportMode="Sea", ConsignmentCountry="KOR"),
    make_snowflake_row(HSCode="91000002", Partner="KOR", Export="Import", Value=1000.0, Port="P6", TransportMode="Sea", ConsignmentCountry="KOR"),
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="91000001", PartnerCode="JPN", DirectionCode="Export", Value=705.0, Port="P5", TransportMode="Air", ConsignmentCountry="JPN"),
    make_clustered_row(HSCode="91000001", PartnerCode="JPN", DirectionCode="Export", Value=805.0, Port="P5", TransportMode="Air", ConsignmentCountry="JPN"),
    make_clustered_row(HSCode="91000002", PartnerCode="KOR", DirectionCode="Import", Value=905.0, Port="P6", TransportMode="Sea", ConsignmentCountry="KOR"),
    make_clustered_row(HSCode="91000002", PartnerCode="KOR", DirectionCode="Import", Value=1005.0, Port="P6", TransportMode="Sea", ConsignmentCountry="KOR"),
]

# ---------------------------------------------------------------------------
# Cas 11 : valeurs manquantes (Value / Currency), cote Snowflake et cote Clustered
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    make_snowflake_row(HSCode="95000001", Value=""),  # valeur Snowflake manquante
    make_snowflake_row(HSCode="95000002", Currency=""),  # devise Snowflake manquante
    make_snowflake_row(HSCode="95000003", Value=1000.0, Currency="USD"),
    make_snowflake_row(HSCode="95000004", Value=1000.0, Currency="USD"),
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="95000001", Value=1000.0),
    make_clustered_row(HSCode="95000002", Value=1000.0),
    make_clustered_row(HSCode="95000003", Value=""),  # valeur Clustered manquante
    make_clustered_row(HSCode="95000004", CurrencyCode=None),  # devise Clustered manquante
]

# ---------------------------------------------------------------------------
# Cas 12 : formats de pays melanges (nom complet vs ISO3), normalises avant jointure
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    # Partner en nom complet cote Snowflake, ISO3 cote Clustered -> doit se normaliser et matcher
    make_snowflake_row(HSCode="96000001", Partner="Switzerland", Value=1000.0),  # vs 1015 -> 1.5% MATCH
    # Reporter en nom complet cote Snowflake, ISO3 (deja "CAN") cote Clustered -> doit se normaliser et matcher
    make_snowflake_row(HSCode="96000002", Reporter="Canada", Value=1000.0),  # vs 800 -> 25% MISMATCH
    # Nom fictif, aucune correspondance possible -> pays_non_reconnu
    make_snowflake_row(HSCode="96000003", Partner="Wakanda", Value=1000.0),
    # Alias manuel COUNTRY_ALIASES : "Ivory Coast" non resolu par pycountry.search_fuzzy seul
    make_snowflake_row(HSCode="96000004", Partner="Ivory Coast", Value=1000.0),  # vs 990 -> 1.0% MATCH
    # Alias manuel COUNTRY_ALIASES : "Turkey" (nom courant) non resolu par pycountry.search_fuzzy seul
    make_snowflake_row(HSCode="96000005", Partner="Turkey", Value=1000.0),  # vs 700 -> 30% MISMATCH
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="96000001", PartnerCode="CHE", Value=1015.0),
    make_clustered_row(HSCode="96000002", ReporterCode="CAN", Value=800.0),
    make_clustered_row(HSCode="96000004", PartnerCode="CIV", Value=990.0),
    make_clustered_row(HSCode="96000005", PartnerCode="TUR", Value=700.0),
]

# ---------------------------------------------------------------------------
# Cas 13 : "Value Unit multiplier" cote Snowflake (Clustered n'a jamais de multiplicateur applique)
# ---------------------------------------------------------------------------
SNOWFLAKE_ROWS += [
    # Multiplicateur vide/NaN -> traite comme 1, adjusted = Value
    make_snowflake_row(HSCode="97000001", Value=1000.0, ValueUnitMultiplier=""),  # vs 1015 -> 1.5% MATCH
    # Multiplicateur = 1 explicite -> meme comportement que le cas NaN
    make_snowflake_row(HSCode="97000002", Value=1000.0, ValueUnitMultiplier=1),  # vs 800 -> 25% MISMATCH
    # Multiplicateur > 1 (1000) -> adjusted = Value * 1000. Sans ca, 2.5 vs 2525 serait un faux MISMATCH enorme.
    make_snowflake_row(HSCode="97000003", Value=2.5, ValueUnitMultiplier=1000),  # adjusted=2500 vs 2525 -> 0.99% MATCH
    # Multiplicateur non numerique (texte invalide) -> traite comme 1, avec avertissement logue
    make_snowflake_row(HSCode="97000004", Value=1000.0, ValueUnitMultiplier="invalid_unit"),  # vs 1010 -> 1.0% MATCH
]
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="97000001", Value=1015.0),
    make_clustered_row(HSCode="97000002", Value=800.0),
    make_clustered_row(HSCode="97000003", Value=2525.0),
    make_clustered_row(HSCode="97000004", Value=1010.0),
]

# ---------------------------------------------------------------------------
# Lignes Clustered orphelines (aucune ligne Snowflake correspondante), pour la realite du fichier
# ---------------------------------------------------------------------------
CLUSTERED_ROWS += [
    make_clustered_row(HSCode="99000001", PartnerCode="AUS", Value=100.0),
    make_clustered_row(HSCode="99000002", PartnerCode="AUS", Value=200.0),
    make_clustered_row(HSCode="99000003", PartnerCode="AUS", Value=300.0),
]


def main() -> None:
    snowflake_df = pd.DataFrame(SNOWFLAKE_ROWS)[list(SNOWFLAKE_DEFAULTS.keys())]
    snowflake_df = snowflake_df.rename(columns={"ValueUnitMultiplier": "Value Unit multiplier"})
    snowflake_df.to_csv("snowflake_data.csv", index=False)

    clustered_csv_rows = [clustered_row_to_csv_dict(row) for row in CLUSTERED_ROWS]
    clustered_df = pd.DataFrame(clustered_csv_rows)
    clustered_df.to_csv("clustered_data.csv", index=False)

    print(f"snowflake_data.csv genere : {len(snowflake_df)} lignes")
    print(f"clustered_data.csv genere : {len(clustered_df)} lignes")


if __name__ == "__main__":
    main()
