# Dnext — comparaison de sources Snowflake

## Installation

```
pip install -r requirements.txt --break-system-packages
```

## Configuration

Ouvre `cred.yaml` (déjà présent à la racine) et remplace les valeurs entre `<...>` par tes identifiants Snowflake :

```yaml
snowflake:
  account: <ACCOUNT>
  user: <USER>
  password: <PASSWORD>
  warehouse: <WAREHOUSE>
  database: <DATABASE>
  schema: <SCHEMA>
```

`cred.yaml` contient un mot de passe en clair : ne jamais le committer ni le partager.

## Lancer la comparaison agrégée

```
python compare_aggregated_sources.py
```

Résultat écrit dans `aggregated_comparison_result.csv`, résumé affiché dans le terminal.
