"""Client Snowflake minimal : connexion via cred.yaml et exécution de requêtes -> pandas DataFrame."""

from __future__ import annotations

import yaml
import snowflake.connector

CRED_FILE = "cred.yaml"


class SnowflakeClientError(Exception):
    """Erreur levée en cas de problème de configuration ou de connexion à Snowflake."""


def _load_credentials(path: str = CRED_FILE) -> dict:
    """Charge les identifiants Snowflake depuis un fichier YAML (clé racine "snowflake")."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError as exc:
        raise SnowflakeClientError(f"Fichier de credentials introuvable : '{path}'.") from exc
    except yaml.YAMLError as exc:
        raise SnowflakeClientError(f"Fichier de credentials illisible (YAML invalide) : '{path}'.") from exc

    if not isinstance(config, dict) or "snowflake" not in config:
        raise SnowflakeClientError(f"Clé 'snowflake' absente de '{path}'.")

    creds = config["snowflake"]
    required = {"account", "user", "password", "warehouse", "database", "schema"}
    missing = required - creds.keys()
    if missing:
        raise SnowflakeClientError(f"Clés manquantes dans '{path}' sous 'snowflake' : {sorted(missing)}.")

    return creds


def get_connection(path: str = CRED_FILE) -> snowflake.connector.SnowflakeConnection:
    """Ouvre une connexion Snowflake à partir des identifiants de `path`.

    Les identifiants (notamment le mot de passe) ne sont jamais inclus dans les messages
    d'erreur ni dans les logs : en cas d'échec de connexion, seul le compte/utilisateur/warehouse
    sont mentionnés.
    """
    creds = _load_credentials(path)

    try:
        return snowflake.connector.connect(
            account=creds["account"],
            user=creds["user"],
            password=creds["password"],
            warehouse=creds["warehouse"],
            database=creds["database"],
            schema=creds["schema"],
        )
    except snowflake.connector.errors.Error as exc:
        raise SnowflakeClientError(
            f"Échec de connexion à Snowflake (account='{creds['account']}', "
            f"user='{creds['user']}', warehouse='{creds['warehouse']}'). "
            "Vérifiez les identifiants dans cred.yaml."
        ) from exc


def run_query(sql: str, path: str = CRED_FILE):
    """Exécute `sql` sur Snowflake et retourne le résultat sous forme de pandas DataFrame."""
    conn = get_connection(path)
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            return cur.fetch_pandas_all()
        finally:
            cur.close()
    except snowflake.connector.errors.Error as exc:
        raise SnowflakeClientError(f"Échec de l'exécution de la requête Snowflake : {exc}") from exc
    finally:
        conn.close()
