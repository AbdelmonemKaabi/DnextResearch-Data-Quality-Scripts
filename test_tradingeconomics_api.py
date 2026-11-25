"""Exploration : verifie l'acces a l'endpoint de donnees historique Trading Economics
(CloudFront) capture via DevTools (onglet Network, clic sur le bouton MAX du graphique),
sans passer par le HTML/graphique de la page.

Script exploratoire, pas de pipeline de production : affiche le statut HTTP et la structure de
la reponse pour qu'on puisse juger si l'acces marche avec seulement x-api-key, et si `span=10y`
suffit a remonter avant 2019 (sinon, tester un autre span, ex: "max" ou une valeur plus grande).

Usage :
    python test_tradingeconomics_api.py [code] [span]

Exemple (valeurs par defaut si non precisees, cas Australie confirme) :
    python test_tradingeconomics_api.py auitexp 10y
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import requests

BASE_URL = "https://d3ii0wo49og5mi.cloudfront.net/economics/{code}"
# Cle capturee via DevTools sur tradingeconomics.com/australia/exports (clic bouton MAX).
API_KEY = "20260324:loboantunes"

DEFAULT_CODE = "auitexp"
DEFAULT_SPAN = "10y"


def fetch(code: str, span: str) -> requests.Response:
    """Appelle l'endpoint CloudFront pour `code` (ex: auitexp) sur la periode `span` (ex: 10y).

    Headers volontairement minimaux (uniquement x-api-key) pour ce premier test : si la reponse
    echoue en 401/403, essayer d'ajouter User-Agent/Referer/Origin (voir la commande curl copiee
    depuis DevTools pour les valeurs exactes utilisees par le navigateur).

    `v` est un parametre horodate observe dans la requete capturee (role exact -- cache-buster ou
    versionning -- non confirme) ; on envoie l'heure UTC courante au meme format.
    """
    url = BASE_URL.format(code=code)
    params = {
        "span": span,
        "v": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
    }
    headers = {"x-api-key": API_KEY}
    return requests.get(url, params=params, headers=headers, timeout=15)


def _extract_dates(data) -> list:
    """Essaie d'extraire les dates de `data`, sans supposer un format precis (liste de paires
    [date, valeur], ou liste de dicts avec une cle Date/date/DateTime/d). Renvoie une liste vide
    si la structure ne correspond a aucun de ces formats -- a inspecter manuellement dans ce cas.
    """
    dates = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, list) and item:
                dates.append(item[0])
            elif isinstance(item, dict):
                for key in ("Date", "date", "DateTime", "d"):
                    if key in item:
                        dates.append(item[key])
                        break
    return dates


def _summarize_response(resp: requests.Response) -> None:
    print(f"Status HTTP : {resp.status_code}")
    print(f"URL appelee : {resp.url}")
    print(f"Content-Type : {resp.headers.get('Content-Type', '?')}")

    if resp.status_code in (401, 403):
        print(
            f"[ECHEC] {resp.status_code} : cle API insuffisante ou header manquant "
            "(User-Agent/Referer/Origin possiblement requis en plus de x-api-key -- "
            "comparer avec la commande curl complete copiee depuis DevTools)."
        )
        print("Corps de la reponse (brut, tronque a 500 caracteres) :")
        print(resp.text[:500])
        return

    if not resp.ok:
        print(f"[ECHEC] Code HTTP inattendu {resp.status_code} -- a diagnostiquer.")
        print("Corps de la reponse (brut, tronque a 500 caracteres) :")
        print(resp.text[:500])
        return

    try:
        data = resp.json()
    except ValueError:
        print("[ECHEC] Reponse 200 mais pas du JSON valide. Corps brut (tronque a 500 caracteres) :")
        print(resp.text[:500])
        return

    print("[OK] Reponse JSON recue.")
    print(f"Type racine : {type(data).__name__}")

    if isinstance(data, list):
        print(f"Nombre d'elements : {len(data)}")
        if data:
            print("Premier element :", data[0])
            print("Dernier element  :", data[-1])
        dates = _extract_dates(data)
        if dates:
            print(f"Date la plus ancienne trouvee : {min(dates)!r}")
            print(f"Date la plus recente trouvee  : {max(dates)!r}")
        else:
            print(
                "[A VERIFIER] Impossible d'identifier automatiquement un champ date dans les "
                "elements -- inspecter 'Premier element'/'Dernier element' ci-dessus a la main."
            )
    elif isinstance(data, dict):
        print(f"Cles racine : {list(data.keys())}")
        print("Extrait (tronque a 1000 caracteres) :")
        print(str(data)[:1000])
    else:
        print("Contenu :", data)


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CODE
    span = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SPAN

    print(f"Test endpoint Trading Economics : code={code!r} span={span!r}\n")
    try:
        response = fetch(code, span)
    except requests.exceptions.RequestException as exc:
        print(f"[ECHEC] Erreur reseau lors de l'appel : {exc}")
        sys.exit(1)

    _summarize_response(response)
