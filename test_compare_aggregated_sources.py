"""Tests (script autonome, sans framework) pour :
  - la regle d'exclusion silencieuse des lignes Snowflake en echec de conversion devise mais
    sans valeur reelle (voir _aggregate_snowflake dans compare_aggregated_sources.py) ;
  - le cas special MOP (pataca de Macao), converti via un peg fixe MOP->HKD (voir
    get_mop_to_usd_rate dans exchange_rate.py).

Usage : python test_compare_aggregated_sources.py
Sort avec le code 1 si une assertion echoue.

Note : test_get_mop_to_usd_rate_coherente fait un appel reseau reel vers l'API Frankfurter
(via HKD) et necessite donc un acces Internet ; les autres tests sont entierement locaux
(donnees synthetiques, get_mop_to_usd_rate mocke).
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pandas as pd

from compare_aggregated_sources import (
    GROUP_KEYS,
    _aggregate_snowflake,
    _count_invalid_chapter_records,
    _find_unrecognized_units,
    compare_aggregated,
)
from exchange_rate import (
    FALLBACK_SOURCE_NAME,
    ExchangeRateError,
    UnsupportedCurrencyError,
    get_monthly_usd_rate,
    get_mop_to_usd_rate,
)

MONTH = pd.Timestamp("2026-01-01")


def _row(
    hs_chapter: str,
    currency: str,
    total_value: float | None,
    number_of_records: int = 1,
    quantity_unit: str = "KG",
    total_quantity: float = 100.0,
) -> dict:
    """Construit une ligne au format attendu par _aggregate_snowflake (sortie SNOWFLAKE_QUERY_TEMPLATE)."""
    return {
        "Month": MONTH,
        "Trade Direction": "Export",
        "HS Chapter": hs_chapter,
        "Quantity Unit": quantity_unit,
        "Quantity 2 Unit": "N/A",
        "Currency": currency,
        "Number of Records": number_of_records,
        "Total Quantity": total_quantity,
        "Total Quantity 2": 0.0,
        "Total Value": total_value,
    }


def _clustered_row(
    hs_chapter: str,
    total_value: float,
    number_of_records: int = 1,
    quantity_unit: str = "KG",
    total_quantity: float = 100.0,
    currency: str = "USD",
) -> dict:
    """Construit une ligne au format attendu par _aggregate_clustered (sortie CLUSTERED_QUERY)."""
    return {
        "Month": MONTH,
        "Trade Direction": "Export",
        "HS Chapter": hs_chapter,
        "Quantity Unit": quantity_unit,
        "Quantity 2 Unit": "N/A",
        "Currency": currency,
        "Number of Records": number_of_records,
        "Total Quantity": total_quantity,
        "Total Quantity 2": 0.0,
        "Total Value": total_value,
    }


def _group(df: pd.DataFrame, hs_chapter: str) -> pd.Series:
    match = df[df["HS Chapter"] == hs_chapter]
    assert len(match) == 1, f"Groupe HS Chapter={hs_chapter!r} : attendu 1 ligne agregee, trouve {len(match)}"
    return match.iloc[0]


def test_devise_manquante_valeur_nulle_exclue_silencieusement() -> None:
    """Une ligne Currency='N/A' avec Total Value=0 ne doit pas contaminer le groupe :
    le reste du groupe (devise valide) doit rester comparable, et l'exclusion doit etre tracee.
    """
    df = pd.DataFrame(
        [
            _row(hs_chapter="08", currency="USD", total_value=1000.0, number_of_records=109),
            _row(hs_chapter="08", currency="N/A", total_value=0.0, number_of_records=1),
        ]
    )

    agg = _aggregate_snowflake(df)
    row = _group(agg, "08")

    assert not math.isnan(row["total_value_snowflake_usd"]), (
        "Le groupe devrait rester comparable (valeur non-NaN) malgre la ligne N/A a valeur nulle"
    )
    assert row["total_value_snowflake_usd"] == 1000.0, (
        f"Attendu 1000.0 (seule la ligne USD contribue), obtenu {row['total_value_snowflake_usd']}"
    )
    assert row["excluded_null_value_rows"] == 1, (
        f"1 enregistrement aurait du etre trace comme exclu silencieusement, obtenu {row['excluded_null_value_rows']}"
    )
    print("OK  test_devise_manquante_valeur_nulle_exclue_silencieusement")


def test_devise_manquante_valeur_reelle_contamine_le_groupe() -> None:
    """Une ligne Currency='N/A' avec une vraie valeur (non nulle) doit toujours faire echouer
    tout le groupe (comportement strict inchange), sans generer d'exclusion silencieuse.
    """
    df = pd.DataFrame(
        [
            _row(hs_chapter="09", currency="USD", total_value=1000.0, number_of_records=50),
            _row(hs_chapter="09", currency="N/A", total_value=500.0, number_of_records=3),
        ]
    )

    agg = _aggregate_snowflake(df)
    row = _group(agg, "09")

    assert math.isnan(row["total_value_snowflake_usd"]), (
        "Le groupe doit rester NON_COMPARABLE (NaN) : une vraie valeur avec devise inconnue "
        "ne peut pas etre ignoree sans fausser le total"
    )
    assert row["excluded_null_value_rows"] == 0, (
        f"Aucune ligne ne doit etre exclue silencieusement ici (la valeur N/A est reelle), "
        f"obtenu {row['excluded_null_value_rows']}"
    )
    print("OK  test_devise_manquante_valeur_reelle_contamine_le_groupe")


def test_groupes_independants() -> None:
    """Les deux groupes (HS 08 et HS 09) ci-dessus doivent etre traites independamment :
    verifie qu'on a bien 2 groupes distincts en sortie, sans interference entre eux.
    """
    df = pd.DataFrame(
        [
            _row(hs_chapter="08", currency="USD", total_value=1000.0, number_of_records=109),
            _row(hs_chapter="08", currency="N/A", total_value=0.0, number_of_records=1),
            _row(hs_chapter="09", currency="USD", total_value=1000.0, number_of_records=50),
            _row(hs_chapter="09", currency="N/A", total_value=500.0, number_of_records=3),
        ]
    )

    agg = _aggregate_snowflake(df)
    assert len(agg) == 2, f"Attendu 2 groupes (HS 08 et HS 09), obtenu {len(agg)}"
    assert set(GROUP_KEYS).issubset(agg.columns), "Colonnes de regroupement manquantes en sortie"
    print("OK  test_groupes_independants")


def test_quantity_zero_des_deux_cotes_est_un_match() -> None:
    """Quand Total Quantity vaut 0 des deux cotes (snowflake ET clustered) pour un groupe,
    c'est un accord parfait (0 = 0) : MATCH, pas NON_COMPARABLE (pas de division par zero
    a signaler ici puisqu'il n'y a aucun ecart reel entre les deux sources).
    """
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="11", currency="USD", total_value=1000.0, number_of_records=5, total_quantity=0.0)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="11", total_value=1000.0, number_of_records=5, total_quantity=0.0)]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    row = result[result["HS Chapter"] == "11"].iloc[0]

    assert row["status_quantity"] == "MATCH", (
        f"Attendu MATCH (0 = 0 des deux cotes), obtenu status={row['status_quantity']!r} "
        f"reason={row['reason_quantity']!r}"
    )
    assert row["pct_diff_quantity"] == 0.0, f"Attendu pct_diff=0.0, obtenu {row['pct_diff_quantity']}"
    assert row["reason_quantity"] == "", f"reason_quantity devrait etre vide, obtenu {row['reason_quantity']!r}"
    print("OK  test_quantity_zero_des_deux_cotes_est_un_match")


def test_quantity_zero_dun_seul_cote_reste_non_comparable() -> None:
    """Regression : quand seul le total clustered est a 0 (snowflake != 0), le comportement
    d'origine doit etre preserve : NON_COMPARABLE (division par zero non sensee), pas MATCH.
    """
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="12", currency="USD", total_value=1000.0, number_of_records=5, total_quantity=100.0)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="12", total_value=1000.0, number_of_records=5, total_quantity=0.0)]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    row = result[result["HS Chapter"] == "12"].iloc[0]

    assert row["status_quantity"] == "NON_COMPARABLE", (
        f"Attendu NON_COMPARABLE (clustered=0, snowflake!=0), obtenu status={row['status_quantity']!r}"
    )
    assert row["reason_quantity"] == "clustered_quantity_zero", (
        f"Attendu reason='clustered_quantity_zero', obtenu {row['reason_quantity']!r}"
    )
    print("OK  test_quantity_zero_dun_seul_cote_reste_non_comparable")


def test_poids_lb_fusionne_avec_kg() -> None:
    """LB doit fusionner avec KG (meme famille poids) : le groupe final doit sommer les deux
    quantites converties en KG, pas rester separe.
    """
    df_snowflake = pd.DataFrame(
        [
            _row(hs_chapter="13", currency="USD", total_value=500.0, number_of_records=2,
                 quantity_unit="LB", total_quantity=10.0),
            _row(hs_chapter="13", currency="USD", total_value=500.0, number_of_records=2,
                 quantity_unit="KG", total_quantity=20.0),
        ]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="13", total_value=1000.0, number_of_records=4,
                         quantity_unit="KG", total_quantity=20.0 + 10.0 * 0.45359237)]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    assert len(result) == 1, f"Attendu 1 seul groupe (LB fusionne avec KG), obtenu {len(result)}"
    row = result.iloc[0]

    assert row["Quantity Unit"] == "KG", f"Attendu unite canonique KG, obtenu {row['Quantity Unit']!r}"
    expected_qty = 20.0 + 10.0 * 0.45359237
    assert abs(row["total_quantity_snowflake"] - expected_qty) < 1e-6, (
        f"Attendu {expected_qty} (10 LB converties + 20 KG), obtenu {row['total_quantity_snowflake']}"
    )
    assert row["status_quantity"] == "MATCH", (
        f"Attendu MATCH, obtenu status={row['status_quantity']!r} reason={row['reason_quantity']!r}"
    )
    print("OK  test_poids_lb_fusionne_avec_kg")


def test_volume_ml_fusionne_avec_l() -> None:
    """ML doit fusionner avec L (meme famille volume), independamment de la famille poids."""
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="14", currency="USD", total_value=200.0, number_of_records=1,
              quantity_unit="ML", total_quantity=5000.0)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="14", total_value=200.0, number_of_records=1,
                         quantity_unit="L", total_quantity=5.0)]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    assert len(result) == 1, f"Attendu 1 seul groupe (ML fusionne avec L), obtenu {len(result)}"
    row = result.iloc[0]

    assert row["Quantity Unit"] == "L", f"Attendu unite canonique L, obtenu {row['Quantity Unit']!r}"
    assert row["status_quantity"] == "MATCH", (
        f"Attendu MATCH (5000 ML = 5 L), obtenu status={row['status_quantity']!r} reason={row['reason_quantity']!r}"
    )
    print("OK  test_volume_ml_fusionne_avec_l")


def test_surface_reste_separee_du_poids() -> None:
    """M2 (surface) et KG (poids) sont deux grandeurs physiques differentes : elles ne doivent
    jamais fusionner, meme au sein du meme groupe (Month/Direction/HS Chapter).
    """
    df_snowflake = pd.DataFrame(
        [
            _row(hs_chapter="15", currency="USD", total_value=300.0, number_of_records=1,
                 quantity_unit="M2", total_quantity=50.0),
            _row(hs_chapter="15", currency="USD", total_value=400.0, number_of_records=1,
                 quantity_unit="KG", total_quantity=60.0),
        ]
    )
    df_clustered = pd.DataFrame(
        [
            _clustered_row(hs_chapter="15", total_value=300.0, number_of_records=1,
                            quantity_unit="M2", total_quantity=50.0),
            _clustered_row(hs_chapter="15", total_value=400.0, number_of_records=1,
                            quantity_unit="KG", total_quantity=60.0),
        ]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    assert len(result) == 2, f"Attendu 2 groupes distincts (M2 et KG ne fusionnent pas), obtenu {len(result)}"
    assert set(result["Quantity Unit"]) == {"M2", "KG"}, (
        f"Attendu les unites M2 et KG separees, obtenu {set(result['Quantity Unit'])}"
    )
    print("OK  test_surface_reste_separee_du_poids")


def test_unite_inconnue_reste_isolee() -> None:
    """Une unite hors de toutes les familles connues (ex: PIECE) reste dans son propre groupe,
    sans etre fusionnee ni avec le poids ni avec une autre unite de comptage.
    """
    df_snowflake = pd.DataFrame(
        [
            _row(hs_chapter="16", currency="USD", total_value=100.0, number_of_records=1,
                 quantity_unit="PIECE", total_quantity=7.0),
            _row(hs_chapter="16", currency="USD", total_value=200.0, number_of_records=1,
                 quantity_unit="UNIT", total_quantity=9.0),
        ]
    )
    df_clustered = pd.DataFrame(
        [
            _clustered_row(hs_chapter="16", total_value=100.0, number_of_records=1,
                            quantity_unit="PIECE", total_quantity=7.0),
            _clustered_row(hs_chapter="16", total_value=200.0, number_of_records=1,
                            quantity_unit="UNIT", total_quantity=9.0),
        ]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    assert len(result) == 2, f"Attendu 2 groupes distincts (PIECE et UNIT non fusionnes), obtenu {len(result)}"
    assert set(result["Quantity Unit"]) == {"PIECE", "UNIT"}, (
        f"Attendu les unites PIECE et UNIT separees, obtenu {set(result['Quantity Unit'])}"
    )
    print("OK  test_unite_inconnue_reste_isolee")


def test_nouvelles_unites_poids_volume_longueur_fusionnent() -> None:
    """GRAM fusionne avec KG (poids), M3/CUFT fusionnent avec L (volume), INCH fusionne avec M
    (longueur) -- verifie les facteurs de conversion ajoutes pour les codes reels decouverts.
    """
    df_snowflake = pd.DataFrame(
        [
            _row(hs_chapter="17", currency="USD", total_value=100.0, number_of_records=1,
                 quantity_unit="GRAM", total_quantity=2000.0),
            _row(hs_chapter="18", currency="USD", total_value=100.0, number_of_records=1,
                 quantity_unit="M3", total_quantity=2.0),
            _row(hs_chapter="19", currency="USD", total_value=100.0, number_of_records=1,
                 quantity_unit="INCH", total_quantity=100.0),
        ]
    )
    df_clustered = pd.DataFrame(
        [
            _clustered_row(hs_chapter="17", total_value=100.0, number_of_records=1,
                            quantity_unit="KG", total_quantity=2.0),
            _clustered_row(hs_chapter="18", total_value=100.0, number_of_records=1,
                            quantity_unit="L", total_quantity=2000.0),
            _clustered_row(hs_chapter="19", total_value=100.0, number_of_records=1,
                            quantity_unit="M", total_quantity=100.0 * 0.0254),
        ]
    )

    result = compare_aggregated(df_snowflake, df_clustered)

    gram_row = result[result["HS Chapter"] == "17"].iloc[0]
    assert gram_row["Quantity Unit"] == "KG" and gram_row["status_quantity"] == "MATCH", (
        f"GRAM->KG : attendu MATCH, obtenu status={gram_row['status_quantity']!r} "
        f"reason={gram_row['reason_quantity']!r}"
    )

    m3_row = result[result["HS Chapter"] == "18"].iloc[0]
    assert m3_row["Quantity Unit"] == "L" and m3_row["status_quantity"] == "MATCH", (
        f"M3->L : attendu MATCH, obtenu status={m3_row['status_quantity']!r} "
        f"reason={m3_row['reason_quantity']!r}"
    )

    inch_row = result[result["HS Chapter"] == "19"].iloc[0]
    assert inch_row["Quantity Unit"] == "M" and inch_row["status_quantity"] == "MATCH", (
        f"INCH->M : attendu MATCH, obtenu status={inch_row['status_quantity']!r} "
        f"reason={inch_row['reason_quantity']!r}"
    )
    print("OK  test_nouvelles_unites_poids_volume_longueur_fusionnent")


def test_unites_ambigues_non_ajoutees_restent_isolees() -> None:
    """GROSS_TONNAGE, TONNE et DMT sont volontairement absentes des tables de conversion (sens
    ou facteur incertain) : elles ne doivent jamais fusionner avec KG/MT.
    """
    df_snowflake = pd.DataFrame(
        [
            _row(hs_chapter="20", currency="USD", total_value=100.0, number_of_records=1,
                 quantity_unit="GROSS_TONNAGE", total_quantity=5.0),
            _row(hs_chapter="20", currency="USD", total_value=200.0, number_of_records=1,
                 quantity_unit="TONNE", total_quantity=3.0),
            _row(hs_chapter="20", currency="USD", total_value=300.0, number_of_records=1,
                 quantity_unit="DMT", total_quantity=4.0),
            _row(hs_chapter="20", currency="USD", total_value=400.0, number_of_records=1,
                 quantity_unit="KG", total_quantity=6.0),
        ]
    )
    df_clustered = pd.DataFrame(
        [
            _clustered_row(hs_chapter="20", total_value=100.0, number_of_records=1,
                            quantity_unit="GROSS_TONNAGE", total_quantity=5.0),
            _clustered_row(hs_chapter="20", total_value=200.0, number_of_records=1,
                            quantity_unit="TONNE", total_quantity=3.0),
            _clustered_row(hs_chapter="20", total_value=300.0, number_of_records=1,
                            quantity_unit="DMT", total_quantity=4.0),
            _clustered_row(hs_chapter="20", total_value=400.0, number_of_records=1,
                            quantity_unit="KG", total_quantity=6.0),
        ]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    assert len(result) == 4, (
        f"Attendu 4 groupes distincts (GROSS_TONNAGE, TONNE, DMT, KG non fusionnes), obtenu {len(result)}"
    )
    assert set(result["Quantity Unit"]) == {"GROSS_TONNAGE", "TONNE", "DMT", "KG"}, (
        f"Attendu 4 unites separees, obtenu {set(result['Quantity Unit'])}"
    )
    print("OK  test_unites_ambigues_non_ajoutees_restent_isolees")


def test_find_unrecognized_units() -> None:
    """_find_unrecognized_units doit ignorer les unites connues (KG) et les unites de comptage
    attendues (UNIT), mais remonter les codes ambigus (GROSS_TONNAGE) et suspects (USD, qui
    ressemble a un code devise egare dans la colonne unite).
    """
    df = pd.DataFrame(
        {"Quantity Unit": ["KG", "unit", "GROSS_TONNAGE", "usd", "kg"]}
    )

    unrecognized = _find_unrecognized_units(df, "Quantity Unit")

    assert unrecognized == {"GROSS_TONNAGE", "USD"}, (
        f"Attendu uniquement {{'GROSS_TONNAGE', 'USD'}}, obtenu {unrecognized}"
    )
    print("OK  test_find_unrecognized_units")


def test_mismatch_petit_ecart_absolu_recoit_le_tag() -> None:
    """MISMATCH avec un ecart absolu minuscule (0.005 < seuil 0.01) doit recevoir le tag
    informatif dans reason_quantity, tout en restant MISMATCH (le statut ne change pas).
    """
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="21", currency="USD", total_value=100.0, number_of_records=1,
              total_quantity=100.005)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="21", total_value=100.0, number_of_records=1, total_quantity=100.0)]
    )

    result = compare_aggregated(df_snowflake, df_clustered, tolerance=0.00001)
    row = result[result["HS Chapter"] == "21"].iloc[0]

    assert row["status_quantity"] == "MISMATCH", (
        f"Attendu MISMATCH (tolerance volontairement tres basse), obtenu {row['status_quantity']!r}"
    )
    assert row["reason_quantity"] == "ecart_absolu_faible_possible_arrondi", (
        f"Attendu le tag d'ecart absolu faible, obtenu {row['reason_quantity']!r}"
    )
    print("OK  test_mismatch_petit_ecart_absolu_recoit_le_tag")


def test_mismatch_gros_ecart_absolu_ne_recoit_pas_le_tag() -> None:
    """MISMATCH avec un ecart absolu franchement superieur au seuil ne doit PAS recevoir le tag."""
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="22", currency="USD", total_value=100.0, number_of_records=1, total_quantity=50.0)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="22", total_value=100.0, number_of_records=1, total_quantity=100.0)]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    row = result[result["HS Chapter"] == "22"].iloc[0]

    assert row["status_quantity"] == "MISMATCH", f"Attendu MISMATCH, obtenu {row['status_quantity']!r}"
    assert row["reason_quantity"] == "", (
        f"Ecart absolu (50.0) largement au-dessus du seuil : reason_quantity devrait rester vide, "
        f"obtenu {row['reason_quantity']!r}"
    )
    print("OK  test_mismatch_gros_ecart_absolu_ne_recoit_pas_le_tag")


def test_get_mop_to_usd_rate_coherente() -> None:
    """Appel reseau reel (Frankfurter, via HKD) : verifie que get_mop_to_usd_rate(2026, 1)
    renvoie un taux MOP->USD coherent (MOP/USD tourne autour de 8, donc rate ~ 1/8 = 0.125)
    et un resultat structure incluant le chemin de peg utilise. Necessite un acces Internet.
    """
    result = get_mop_to_usd_rate(2026, 1)

    assert 0.10 <= result["rate"] <= 0.14, (
        f"Taux MOP->USD hors fourchette attendue (~1/8 = 0.125) : {result['rate']}"
    )
    assert result["days_used"] > 0, "Aucun jour ouvrable utilise pour le calcul du taux"
    assert result["via_peg"] == "MOP->HKD fixed, HKD->USD Frankfurter", (
        f"Chemin de peg inattendu : {result['via_peg']!r}"
    )
    print(f"OK  test_get_mop_to_usd_rate_coherente (rate={result['rate']:.5f})")


def test_conversion_mop_passe_de_non_comparable_a_match() -> None:
    """Cas synthetique : une ligne Snowflake en MOP avec une valeur reelle doit maintenant se
    convertir (via le peg MOP->HKD) et comparer normalement, au lieu de tomber en
    NON_COMPARABLE avec "conversion_echouee: MOP: Devise invalide ou inconnue pour
    Frankfurter". get_mop_to_usd_rate est mocke pour ne pas dependre du reseau ici.
    """
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="10", currency="MOP", total_value=800.0, number_of_records=20)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="10", total_value=100.0, number_of_records=20)]
    )

    mocked_rate = {
        "rate": 0.125,  # ~ 1 MOP = 0.125 USD (MOP/USD ~ 8)
        "days_used": 20,
        "dates": [],
        "via_peg": "MOP->HKD fixed, HKD->USD Frankfurter",
    }
    with patch("compare_aggregated_sources.get_mop_to_usd_rate", return_value=mocked_rate) as mocked:
        result = compare_aggregated(df_snowflake, df_clustered)
        mocked.assert_called_once_with(2026, 1)

    row = result[result["HS Chapter"] == "10"].iloc[0]

    assert row["status_value"] == "MATCH", (
        f"Attendu MATCH (800 MOP * 0.125 = 100 USD = clustered), obtenu "
        f"status={row['status_value']!r} reason={row['reason_value']!r}"
    )
    assert row["reason_value"] == "", f"reason_value devrait etre vide, obtenu {row['reason_value']!r}"
    assert row["total_value_snowflake_usd"] == 100.0, (
        f"Attendu 100.0 USD, obtenu {row['total_value_snowflake_usd']}"
    )
    print("OK  test_conversion_mop_passe_de_non_comparable_a_match")


def test_groupe_present_uniquement_dans_clustered_apparait() -> None:
    """Un groupe present seulement cote Clustered doit apparaitre dans le resultat (jointure
    externe), en NON_COMPARABLE sur toutes les metriques avec la raison absent_snowflake, au lieu
    d'etre ignore silencieusement.
    """
    df_snowflake = pd.DataFrame([_row(hs_chapter="30", currency="USD", total_value=100.0)])
    df_clustered = pd.DataFrame(
        [
            _clustered_row(hs_chapter="30", total_value=100.0),
            _clustered_row(hs_chapter="31", total_value=999999.0),
        ]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    assert len(result) == 2, f"Attendu 2 groupes (commun + Clustered seul), obtenu {len(result)}"

    row = _group(result, "31")
    assert row["presence"] == "clustered_only", f"Attendu presence=clustered_only, obtenu {row['presence']!r}"
    for metric in ["records", "quantity", "quantity2", "value"]:
        assert row[f"status_{metric}"] == "NON_COMPARABLE", (
            f"{metric} : attendu NON_COMPARABLE, obtenu {row[f'status_{metric}']!r}"
        )
        assert row[f"reason_{metric}"] == "absent_snowflake", (
            f"{metric} : attendu reason='absent_snowflake', obtenu {row[f'reason_{metric}']!r}"
        )
    assert row["total_value_clustered_usd"] == 999999.0, "La valeur Clustered doit rester visible"
    assert _group(result, "30")["presence"] == "both"
    print("OK  test_groupe_present_uniquement_dans_clustered_apparait")


def test_groupe_present_uniquement_dans_snowflake_apparait() -> None:
    """Un groupe present seulement cote Snowflake doit apparaitre dans le resultat (et plus
    seulement etre compte dans le resume), en NON_COMPARABLE avec la raison absent_clustered.
    """
    df_snowflake = pd.DataFrame(
        [
            _row(hs_chapter="32", currency="USD", total_value=100.0),
            _row(hs_chapter="33", currency="USD", total_value=5000.0, number_of_records=7),
        ]
    )
    df_clustered = pd.DataFrame([_clustered_row(hs_chapter="32", total_value=100.0)])

    result = compare_aggregated(df_snowflake, df_clustered)
    row = _group(result, "33")
    assert row["presence"] == "snowflake_only", f"Attendu presence=snowflake_only, obtenu {row['presence']!r}"
    assert row["status_value"] == "NON_COMPARABLE" and row["reason_value"] == "absent_clustered", (
        f"Attendu NON_COMPARABLE/absent_clustered, obtenu {row['status_value']!r}/{row['reason_value']!r}"
    )
    assert row["number_of_records_snowflake"] == 7, "Le nombre d'enregistrements Snowflake doit rester visible"
    assert pd.isna(row["number_of_records_clustered"]), "Aucun enregistrement Clustered attendu"
    print("OK  test_groupe_present_uniquement_dans_snowflake_apparait")


def test_devise_non_usd_cote_clustered_rend_la_valeur_non_comparable() -> None:
    """Une ligne Clustered en EUR avec une vraie valeur ne doit plus etre sommee comme des USD :
    la valeur du groupe devient NON_COMPARABLE avec la devise fautive dans la raison. Les
    quantites, elles, restent comparees normalement.
    """
    df_snowflake = pd.DataFrame([_row(hs_chapter="34", currency="USD", total_value=100.0, total_quantity=200.0)])
    df_clustered = pd.DataFrame(
        [
            _clustered_row(hs_chapter="34", total_value=50.0, currency="USD"),
            _clustered_row(hs_chapter="34", total_value=50.0, currency="EUR"),
        ]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    row = _group(result, "34")
    assert row["status_value"] == "NON_COMPARABLE", (
        f"Attendu NON_COMPARABLE (EUR somme comme USD), obtenu {row['status_value']!r}"
    )
    assert row["reason_value"] == "clustered_devise_non_usd: EUR", (
        f"Attendu reason='clustered_devise_non_usd: EUR', obtenu {row['reason_value']!r}"
    )
    assert row["status_quantity"] == "MATCH", (
        f"La quantite (200 = 100 + 100) doit rester comparee, obtenu {row['status_quantity']!r}"
    )
    print("OK  test_devise_non_usd_cote_clustered_rend_la_valeur_non_comparable")


def test_devise_clustered_tolerees() -> None:
    """Cote Clustered, une devise non renseignee (N/A) est toleree comme USD (hypothese documentee),
    et une devise etrangere sur une ligne a valeur nulle ne contamine pas le groupe.
    """
    df_snowflake = pd.DataFrame([_row(hs_chapter="35", currency="USD", total_value=100.0, total_quantity=300.0)])
    df_clustered = pd.DataFrame(
        [
            _clustered_row(hs_chapter="35", total_value=60.0, currency="USD"),
            _clustered_row(hs_chapter="35", total_value=40.0, currency="N/A"),
            _clustered_row(hs_chapter="35", total_value=0.0, currency="EUR"),
        ]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    row = _group(result, "35")
    assert row["status_value"] == "MATCH" and row["reason_value"] == "", (
        f"Attendu MATCH sans raison, obtenu {row['status_value']!r}/{row['reason_value']!r}"
    )
    print("OK  test_devise_clustered_tolerees")


def test_nombre_enregistrements_est_compare() -> None:
    """Un ecart de nombre d'enregistrements (1 vs 500) doit donner MISMATCH sur status_records,
    meme si valeur et quantite concordent ; un nombre identique doit donner MATCH.
    """
    df_snowflake = pd.DataFrame(
        [
            _row(hs_chapter="36", currency="USD", total_value=100.0, number_of_records=1),
            _row(hs_chapter="37", currency="USD", total_value=100.0, number_of_records=42),
        ]
    )
    df_clustered = pd.DataFrame(
        [
            _clustered_row(hs_chapter="36", total_value=100.0, number_of_records=500),
            _clustered_row(hs_chapter="37", total_value=100.0, number_of_records=42),
        ]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    row = _group(result, "36")
    assert row["status_records"] == "MISMATCH", f"Attendu MISMATCH (1 vs 500), obtenu {row['status_records']!r}"
    assert row["status_value"] == "MATCH" and row["status_quantity"] == "MATCH"
    assert _group(result, "37")["status_records"] == "MATCH", "42 vs 42 : attendu MATCH"
    print("OK  test_nombre_enregistrements_est_compare")


def test_count_invalid_chapter_records() -> None:
    """Seuls les chapitres hors 01-99 ('00', ou un code d'un seul chiffre comme '1') sont comptes."""
    df = pd.DataFrame(
        {"HS Chapter": ["00", "84", "00", "01", "1", "99"], "Number of Records": [3, 10, 2, 5, 7, 4]}
    )
    n_invalid = _count_invalid_chapter_records(df)
    assert n_invalid == 12, f"Attendu 12 enregistrements invalides ('00' x2 et '1'), obtenu {n_invalid}"
    print("OK  test_count_invalid_chapter_records")


def test_dexp_et_reex_fusionnes_en_export() -> None:
    """Cote Snowflake, DEXP (exportations domestiques) et REEX (reexportations) doivent etre
    ramenes a "Export" comme dans Clustered : un seul groupe, present des deux cotes, qui concorde.
    """
    df_snowflake = pd.DataFrame(
        [
            {**_row(hs_chapter="40", currency="USD", total_value=70.0, total_quantity=60.0), "Trade Direction": "DEXP"},
            {**_row(hs_chapter="40", currency="USD", total_value=30.0, total_quantity=40.0), "Trade Direction": "REEX"},
        ]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="40", total_value=100.0, number_of_records=2, total_quantity=100.0)]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    assert len(result) == 1, f"Attendu 1 groupe (DEXP + REEX = Export), obtenu {len(result)}"
    row = result.iloc[0]
    assert row["Trade Direction"] == "Export" and row["presence"] == "both", (
        f"Attendu Export present des deux cotes, obtenu {row['Trade Direction']!r} / {row['presence']!r}"
    )
    assert row["status_value"] == "MATCH" and row["status_quantity"] == "MATCH", (
        f"Attendu MATCH, obtenu value={row['status_value']!r} quantity={row['status_quantity']!r}"
    )
    print("OK  test_dexp_et_reex_fusionnes_en_export")


def test_devise_a_parite_fixe_convertie_sans_reseau() -> None:
    """SAR (3.75 SAR = 1 USD) doit se convertir via la parite fixe, sans appel reseau : 375 SAR =
    100 USD = clustered -> MATCH.
    """
    df_snowflake = pd.DataFrame([_row(hs_chapter="41", currency="SAR", total_value=375.0)])
    df_clustered = pd.DataFrame([_clustered_row(hs_chapter="41", total_value=100.0)])

    with patch("exchange_rate.requests.get", side_effect=AssertionError("aucun appel reseau attendu")):
        result = compare_aggregated(df_snowflake, df_clustered)
    row = result.iloc[0]
    assert row["status_value"] == "MATCH", (
        f"Attendu MATCH (375 SAR = 100 USD), obtenu {row['status_value']!r} reason={row['reason_value']!r}"
    )
    assert abs(row["total_value_snowflake_usd"] - 100.0) < 1e-9
    print("OK  test_devise_a_parite_fixe_convertie_sans_reseau")


def test_source_de_secours_si_frankfurter_ne_connait_pas_la_devise() -> None:
    """Devise inconnue de Frankfurter : get_monthly_usd_rate doit basculer sur currency-api et
    moyenner les jours ouvrables du mois (22 en janvier 2026). Code devise fictif (XAA) pour ne pas
    polluer le cache d'une vraie devise.
    """
    with patch("exchange_rate.get_monthly_average_rate", side_effect=UnsupportedCurrencyError("inconnue")), \
            patch("exchange_rate._get_daily_rate_fallback", return_value=0.5) as daily:
        info = get_monthly_usd_rate("XAA", 2026, 1)

    assert info["rate"] == 0.5 and info["source"] == FALLBACK_SOURCE_NAME, f"Resultat inattendu : {info}"
    assert info["days_used"] == 22 and daily.call_count == 22, (
        f"Attendu 22 jours ouvrables en janvier 2026, obtenu days_used={info['days_used']} appels={daily.call_count}"
    )
    print("OK  test_source_de_secours_si_frankfurter_ne_connait_pas_la_devise")


def test_echec_source_de_secours_memorise() -> None:
    """Si la source de secours n'a aucun taux pour un mois, l'echec doit etre memorise : un second
    appel echoue aussitot, sans refaire les ~20 requetes (sinon une requete par ligne agregee).
    """
    with patch("exchange_rate.get_monthly_average_rate", side_effect=UnsupportedCurrencyError("inconnue")), \
            patch("exchange_rate._get_daily_rate_fallback", return_value=None) as daily:
        for _ in range(2):
            try:
                get_monthly_usd_rate("XBB", 2026, 1)
                raise AssertionError("ExchangeRateError attendue (aucun taux disponible)")
            except ExchangeRateError:
                pass

    assert daily.call_count == 22, f"Attendu 22 requetes au total (premier appel seulement), obtenu {daily.call_count}"
    print("OK  test_echec_source_de_secours_memorise")


if __name__ == "__main__":
    tests = [
        test_devise_manquante_valeur_nulle_exclue_silencieusement,
        test_devise_manquante_valeur_reelle_contamine_le_groupe,
        test_groupes_independants,
        test_conversion_mop_passe_de_non_comparable_a_match,
        test_quantity_zero_des_deux_cotes_est_un_match,
        test_quantity_zero_dun_seul_cote_reste_non_comparable,
        test_poids_lb_fusionne_avec_kg,
        test_volume_ml_fusionne_avec_l,
        test_surface_reste_separee_du_poids,
        test_unite_inconnue_reste_isolee,
        test_nouvelles_unites_poids_volume_longueur_fusionnent,
        test_unites_ambigues_non_ajoutees_restent_isolees,
        test_find_unrecognized_units,
        test_mismatch_petit_ecart_absolu_recoit_le_tag,
        test_mismatch_gros_ecart_absolu_ne_recoit_pas_le_tag,
        test_groupe_present_uniquement_dans_clustered_apparait,
        test_groupe_present_uniquement_dans_snowflake_apparait,
        test_devise_non_usd_cote_clustered_rend_la_valeur_non_comparable,
        test_devise_clustered_tolerees,
        test_nombre_enregistrements_est_compare,
        test_count_invalid_chapter_records,
        test_dexp_et_reex_fusionnes_en_export,
        test_devise_a_parite_fixe_convertie_sans_reseau,
        test_source_de_secours_si_frankfurter_ne_connait_pas_la_devise,
        test_echec_source_de_secours_memorise,
        test_get_mop_to_usd_rate_coherente,
    ]
    failures = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append(test.__name__)
            print(f"FAIL {test.__name__}: {exc}")

    print(f"\n{len(tests) - len(failures)}/{len(tests)} tests passes")
    if failures:
        raise SystemExit(1)
