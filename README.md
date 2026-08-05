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

## Choisir le(s) dataset(s)

Le(s) dataset(s) à comparer correspondent aux valeurs de la colonne `DataSource` côté Clustered. Deux façons de les choisir, la ligne de commande étant prioritaire sur le fichier de config :

- **Fichier `datasets_config.yaml`** (déjà présent à la racine) : édite la clé `datasets` avec un dataset, une liste de datasets, ou `all` pour tous les traiter.
- **Argument en ligne de commande**, ignore le fichier de config s'il est fourni :
  ```
  python compare_aggregated_sources.py dnexr-all-che-tf-customs-bilateral-monthly
  python compare_aggregated_sources.py dataset1 dataset2
  python compare_aggregated_sources.py all
  ```

## Lancer la comparaison agrégée

```
python compare_aggregated_sources.py
```

Un dataset en échec (table Snowflake introuvable, etc.) est loggé et ignoré, sans bloquer les autres. Résultat combiné écrit dans `aggregated_comparison_result.csv` (colonne `Dataset` en premier), résumé affiché dans le terminal pour chaque dataset.
