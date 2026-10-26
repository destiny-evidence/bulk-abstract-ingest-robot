# Bulk abstract ingest robot

Boosts abstract coverage in the [DESTinY Open Search index](https://github.com/destiny-evidence/destiny-repository) by taking large result sets exported from academic databases (currently Scopus) and offering their abstracts to references that are missing one — or whose current abstract looks broken.

This is a deliberately pragmatic, **temporary** measure until the proper ingestion flow with full deduplication and data fusion is in place: records are matched by identifier only, existing references are enhanced (never created), and nothing fancy happens beyond a plausibility check that the source record really is the same paper and its abstract adds something new.

## How it works

Three small runner processes work through the pipeline, coordinating only via a Postgres metadata cache and the repository itself — every step is tracked in the database, so any runner can crash and resume where it left off:

1. **Queue runner** — streams JSONL export files from Azure blob storage into the cache, with checkpoints.
2. **Match runner** — looks records up in the repository by OpenAlex ID, filters out implausible matches and duplicates, and requests enhancements for what's left.
3. **Enhance runner** — picks up repository-approved batches and submits the abstract (plus the untouched raw metadata) as enhancements.

```plain
academic database export   ──▶   blob storage  ──▶ [queue]  ──▶  Postgres cache
         [enhance] ◀── approved batches ◀── DESTinY repository ◀── [match] ◀──┘
```

## Documentation

Full motivation, architecture, data model, enhancement rules, and configuration live in the [documentation](docs/docs/index.md).

Quick start:

```sh
uv sync
cp .config/.env.example .env   # fill in robot + keycloak credentials
docker compose up --build      # Postgres + match + enhance runners
```

Run documentation locally:

```sh
uv run zensical serve --config-file docs/zensical.toml --dev-addr localhost:8050
```
