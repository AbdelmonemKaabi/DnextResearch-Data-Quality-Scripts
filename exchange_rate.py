"""Calcul du taux de change mensuel moyen (devise -> USD) : API Frankfurter, parites fixes avec
l'USD, et API currency-api en secours pour les devises inconnues de Frankfurter (voir
get_monthly_usd_rate)."""

from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import lru_cache

import pandas as pd
import requests
import truststore

# Verifie les certificats HTTPS avec le magasin du systeme (Windows) plutot qu'avec le bundle
# certifi de Python. Necessaire derriere un antivirus/proxy qui inspecte le HTTPS (ex: Avast,
# qui re-signe le trafic avec son propre certificat racine, approuve par Windows mais inconnu de
# certifi) : sans cela, chaque appel a Frankfurter echoue en CERTIFICATE_VERIFY_FAILED et toute
# valeur non-USD devient NON_COMPARABLE. Effet global sur le processus (tout le module ssl).
truststore.inject_into_ssl()

FRANKFURTER_BASE_URL = "https://api.frankfurter.app"

# Devises refusees par Frankfurter (HTTP 404 : devise hors des ~31 devises de reference BCE, ex:
# ETB). Reponse deterministe, quel que soit le mois : memorisee pour la duree du processus, sinon
# lru_cache (qui ne cache pas les exceptions) refait une requete d'environ 1 s pour chaque ligne
# agregee de cette devise -- des milliers de requetes inutiles sur un gros dataset. Les autres
# echecs (reseau, HTTP 5xx...) restent non memorises et seront retentes.
_UNSUPPORTED_CURRENCIES: set[str] = set()


class ExchangeRateError(Exception):
    """Erreur levée lorsque le taux de change mensuel ne peut pas être calculé."""


class UnsupportedCurrencyError(ExchangeRateError):
    """Devise inconnue de l'API Frankfurter (HTTP 404) : get_monthly_usd_rate bascule alors sur la
    source de secours (currency-api)."""


@lru_cache(maxsize=None)
def get_monthly_average_rate(
    currency: str, year: int, month: int, timeout: int = 10
) -> dict:
    """Calcule le taux de change mensuel moyen d'une devise vers USD.

    Récupère les taux journaliers disponibles auprès de l'API Frankfurter
    pour tous les jours ouvrables (lundi-vendredi) du mois donné, puis
    calcule la moyenne arithmétique de ces taux.

    Args:
        currency: code ISO de la devise source (ex: "EUR", "TRY", "JPY").
        year: année (ex: 2010).
        month: mois entre 1 et 12 (ex: 1 pour janvier).
        timeout: délai max en secondes pour la requête HTTP.

    Returns:
        Un dict avec les clés:
            - "rate": taux de change moyen (float)
            - "days_used": nombre de jours ouvrables utilisés dans le calcul (int)
            - "dates": liste des dates (str "YYYY-MM-DD") effectivement utilisées

    Raises:
        ExchangeRateError: si la devise est invalide, si l'API est
            injoignable, ou si aucun taux n'a pu être récupéré pour le mois.

    Note:
        Le résultat est mis en cache (par currency/year/month/timeout) via
        `lru_cache` : les appels répétés pour la même devise et le même mois
        ne déclenchent pas de nouvelle requête HTTP. Les appels qui lèvent
        une exception ne sont pas mis en cache et seront retentés.
    """
    currency = currency.upper().strip()
    if currency in _UNSUPPORTED_CURRENCIES:
        raise UnsupportedCurrencyError(f"Devise invalide ou inconnue pour Frankfurter : '{currency}'.")

    business_days = pd.bdate_range(
        start=date(year, month, 1),
        end=date(year, month, calendar.monthrange(year, month)[1]),
    )
    if len(business_days) == 0:
        raise ExchangeRateError(f"Aucun jour ouvrable trouvé pour {year}-{month:02d}.")

    business_day_strs = {d.strftime("%Y-%m-%d") for d in business_days}
    start_str = business_days[0].strftime("%Y-%m-%d")
    end_str = business_days[-1].strftime("%Y-%m-%d")

    url = f"{FRANKFURTER_BASE_URL}/{start_str}..{end_str}"
    params = {"from": currency, "to": "USD"}

    try:
        response = requests.get(url, params=params, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise ExchangeRateError(
            f"Impossible de contacter l'API Frankfurter : {exc}"
        ) from exc

    if response.status_code == 404:
        _UNSUPPORTED_CURRENCIES.add(currency)
        raise UnsupportedCurrencyError(
            f"Devise invalide ou inconnue pour Frankfurter : '{currency}'."
        )
    if not response.ok:
        raise ExchangeRateError(
            f"L'API Frankfurter a renvoyé une erreur HTTP {response.status_code} : "
            f"{response.text}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ExchangeRateError(
            "Réponse de l'API Frankfurter illisible (JSON invalide)."
        ) from exc

    rates_by_date = payload.get("rates", {})
    if not rates_by_date:
        raise ExchangeRateError(
            f"Aucun taux disponible pour {currency}->USD sur {year}-{month:02d}."
        )

    daily_rates = {}
    for day_str, day_rates in rates_by_date.items():
        if day_str not in business_day_strs:
            continue
        usd_rate = day_rates.get("USD")
        if usd_rate is not None:
            daily_rates[day_str] = usd_rate

    if not daily_rates:
        raise ExchangeRateError(
            f"Aucun taux exploitable pour {currency}->USD sur {year}-{month:02d} "
            "(jours fériés ou données manquantes pour tout le mois)."
        )

    rates_series = pd.Series(daily_rates)
    average_rate = float(rates_series.mean())
    used_dates = sorted(daily_rates.keys())

    return {
        "rate": average_rate,
        "days_used": len(used_dates),
        "dates": used_dates,
    }


# Le MOP (pataca de Macao) est indexe de facon fixe au HKD depuis 1983
# via un systeme de currency board (Autorite Monetaire de Macao).
# Taux officiel : 1 HKD = 1.03 MOP. Ce n'est pas un taux de marche,
# c'est une politique monetaire stable depuis plus de 40 ans, mais a
# revoir si Macao changeait de regime monetaire.
MOP_TO_HKD_FIXED_RATE = 1 / 1.03  # 1 MOP = ~0.970874 HKD


def get_mop_to_usd_rate(year: int, month: int, timeout: int = 10) -> dict:
    """Calcule le taux de change mensuel moyen MOP -> USD via un peg fixe MOP -> HKD.

    Frankfurter ne couvre que les ~31 devises de reference de la BCE et ne connait pas le MOP
    (pataca de Macao). On passe donc par le HKD (couvert par Frankfurter), auquel le MOP est
    indexe de facon fixe (voir MOP_TO_HKD_FIXED_RATE) : taux_MOP_USD = MOP_TO_HKD_FIXED_RATE *
    taux_HKD_USD.

    Args:
        year: année (ex: 2026).
        month: mois entre 1 et 12.
        timeout: délai max en secondes pour la requête HTTP (transmis à get_monthly_average_rate).

    Returns:
        Un dict avec les mêmes clés que get_monthly_average_rate ("rate", "days_used", "dates"),
        plus "via_peg" qui documente le chemin de conversion utilisé.

    Raises:
        ExchangeRateError: si le taux HKD -> USD ne peut pas être récupéré (propagée telle
            quelle depuis get_monthly_average_rate, sans être masquée).
    """
    hkd_rate_info = get_monthly_average_rate("HKD", year, month, timeout=timeout)
    return {
        "rate": MOP_TO_HKD_FIXED_RATE * hkd_rate_info["rate"],
        "days_used": hkd_rate_info["days_used"],
        "dates": hkd_rate_info["dates"],
        "via_peg": "MOP->HKD fixed, HKD->USD Frankfurter",
    }


# Devises a parite fixe avec l'USD, absentes de Frankfurter : taux officiels des banques centrales
# (regimes de change fixes en place depuis des decennies), a revoir si l'un d'eux change de regime.
# Valeur = nombre d'USD pour 1 unite de la devise.
USD_PEGGED_RATES = {
    "SAR": 1 / 3.75,  # riyal saoudien : 3.75 SAR = 1 USD
    "QAR": 1 / 3.64,  # riyal qatari : 3.64 QAR = 1 USD
    "BHD": 1 / 0.376,  # dinar bahreini : 0.376 BHD = 1 USD
    "BZD": 1 / 2.0,  # dollar belizien : 2.00 BZD = 1 USD
    "PAB": 1.0,  # balboa panameen : 1 PAB = 1 USD
}

# Source de secours pour les devises inconnues de Frankfurter (ex: EGP, ETB, MAD, NGN, PKR, TND,
# TTD) : API currency-api (projet open source fawazahmed0/exchange-api), gratuite, sans cle, avec
# un historique journalier. Deux miroirs du meme jeu de donnees, essayes dans cet ordre.
FALLBACK_URL_TEMPLATES = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{day}/v1/currencies/{currency}.json",
    "https://{day}.currency-api.pages.dev/v1/currencies/{currency}.json",
)
FALLBACK_SOURCE_NAME = "currency-api (fawazahmed0)"

# Echecs de la source de secours, memorises par (devise, annee, mois) pour la duree du processus,
# pour la meme raison que _UNSUPPORTED_CURRENCIES : ne pas refaire ~20 requetes par ligne agregee.
_FALLBACK_FAILURES: dict[tuple[str, int, int], str] = {}


def _get_daily_rate_fallback(currency: str, day: str, timeout: int = 10) -> float | None:
    """Taux devise -> USD du jour `day` ("YYYY-MM-DD") via currency-api, ou None si indisponible."""
    code = currency.lower()
    for template in FALLBACK_URL_TEMPLATES:
        try:
            response = requests.get(template.format(day=day, currency=code), timeout=timeout)
        except requests.exceptions.RequestException:
            continue
        if not response.ok:
            continue
        try:
            usd_rate = response.json().get(code, {}).get("usd")
        except ValueError:
            continue
        if usd_rate is not None:
            return float(usd_rate)
    return None


@lru_cache(maxsize=None)
def _get_monthly_average_rate_fallback_cached(currency: str, year: int, month: int, timeout: int) -> dict:
    """Moyenne des taux journaliers currency-api sur les jours ouvrables du mois (meme methode que
    get_monthly_average_rate). Jours recuperes en parallele : ~20 requetes HTTP par devise et mois."""
    business_days = [
        d.strftime("%Y-%m-%d")
        for d in pd.bdate_range(start=date(year, month, 1), end=date(year, month, calendar.monthrange(year, month)[1]))
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rates = pool.map(lambda day: _get_daily_rate_fallback(currency, day, timeout), business_days)
        daily_rates = {day: rate for day, rate in zip(business_days, rates) if rate is not None}

    if not daily_rates:
        raise ExchangeRateError(
            f"Aucun taux {currency}->USD disponible sur {year}-{month:02d} ({FALLBACK_SOURCE_NAME})."
        )
    used_dates = sorted(daily_rates)
    return {
        "rate": float(pd.Series(daily_rates).mean()),
        "days_used": len(used_dates),
        "dates": used_dates,
        "source": FALLBACK_SOURCE_NAME,
    }


def _get_monthly_average_rate_fallback(currency: str, year: int, month: int, timeout: int = 10) -> dict:
    """Taux mensuel via la source de secours, avec memorisation des echecs (_FALLBACK_FAILURES)."""
    key = (currency, year, month)
    if key in _FALLBACK_FAILURES:
        raise ExchangeRateError(_FALLBACK_FAILURES[key])
    try:
        return _get_monthly_average_rate_fallback_cached(currency, year, month, timeout)
    except ExchangeRateError as exc:
        _FALLBACK_FAILURES[key] = str(exc)
        raise


def get_monthly_usd_rate(currency: str, year: int, month: int, timeout: int = 10) -> dict:
    """Taux de change mensuel moyen devise -> USD, en essayant les sources dans cet ordre :
      1. parite fixe avec l'USD (USD_PEGGED_RATES) ;
      2. API Frankfurter (get_monthly_average_rate : ~31 devises de reference BCE) ;
      3. si Frankfurter ne connait pas la devise (UnsupportedCurrencyError) : currency-api, avec la
         meme methode (moyenne des taux des jours ouvrables du mois).

    Renvoie le dict de la source utilisee ("rate", "days_used", "dates"), plus une cle "source".

    Raises:
        ExchangeRateError: si aucune source ne fournit de taux pour cette devise et ce mois.
    """
    currency = currency.upper().strip()
    if currency in USD_PEGGED_RATES:
        return {"rate": USD_PEGGED_RATES[currency], "days_used": 0, "dates": [], "source": "parite fixe USD"}
    try:
        return {**get_monthly_average_rate(currency, year, month, timeout=timeout), "source": "Frankfurter"}
    except UnsupportedCurrencyError:
        return _get_monthly_average_rate_fallback(currency, year, month, timeout)


if __name__ == "__main__":
    result = get_monthly_average_rate("EUR", 2010, 1)
    print(f"Taux moyen EUR->USD (2010-01) : {result['rate']:.5f}")
    print(f"Jours utilisés : {result['days_used']}")
    print(f"Dates : {result['dates']}")
