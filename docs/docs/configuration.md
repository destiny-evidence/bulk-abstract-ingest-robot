# Configuration

All configuration is read from environment variables via `pydantic-settings` (`app/util/config.py`). Settings are loaded from (in order of increasing precedence) `.config/.env.secret`, `.config/.env`, `.env.secret`, `.env`, and the actual process environment. Unknown variables are ignored. The canonical list of options is the `Settings` class — the table below mirrors it.

## Repository & authentication

| Variable                          | Default                                       | Description                                                                                                                                       |
| --------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BASE_URL`                        | `https://api.staging.evidence-repository.org` | DESTinY repository API endpoint.                                                                                                                  |
| `ROBOT_ID`                        | — (**required**)                              | Robot client ID (UUID), issued at [robot registration](https://destiny-evidence.github.io/destiny-repository/procedures/robot-registration.html). |
| `ROBOT_SECRET`                    | — (**required**)                              | Secret for HMAC-signing robot API calls.                                                                                                          |
| `KEYCLOAK_ID` / `KEYCLOAK_SECRET` | — (required at runtime)                       | Keycloak client credentials used for reference lookups and enhancement requests.                                                                  |
| `KEYCLOAK_URL`                    | `https://auth.evidence-repository.org/`       | Keycloak server.                                                                                                                                  |
| `KEYCLOAK_REALM`                  | `destiny`                                     | Keycloak realm.                                                                                                                                   |
| `ENV`                             | `staging`                                     | Deployment label (`local`, `development`, `staging`, `test`). Informational — the robot always authenticates.                                     |
| `REPOSITORY_PROVENANCE`           | `PIK Metadata Cache`                          | Provenance/source string attached to submitted enhancements.                                                                                      |

<!-- prettier-ignore -->
!!! info "Robot identity"
    `ROBOT_NAME` and `ROBOT_VERSION` are derived from `pyproject.toml` and only need overriding in exceptional cases.

## Database (metadata cache)

| Variable      | Default      | Description                                       |
| ------------- | ------------ | ------------------------------------------------- |
| `DB_HOST`     | `localhost`  | Postgres host (`postgres` inside Docker Compose). |
| `DB_PORT`     | `5432`       | Postgres port.                                    |
| `DB_NAME`     | `references` | Database name.                                    |
| `DB_USER`     | `pguser`     | Database user.                                    |
| `DB_PASSWORD` | `password`   | Database password.                                |

<!-- prettier-ignore -->
!!! warning "Keep `DB_NAME` consistent"
    The code default is `references`, but the `postgres` service in `compose.yaml` creates the database from `${DB_NAME:-meta_cache}`. Set `DB_NAME` explicitly in `.env` so the robot and the container agree.

## Runner pacing & batching

| Variable                        | Default | Description                                                                |
| ------------------------------- | ------- | -------------------------------------------------------------------------- |
| `MATCH_INTERVAL_SECONDS`        | `30`    | Sleep between match-loop iterations.                                       |
| `ENHANCE_INTERVAL_SECONDS`      | `30`    | Sleep between enhance-loop iterations.                                     |
| `QUEUE_INTERVAL_SECONDS`        | `3600`  | Sleep between checks for new transfer requests.                            |
| `REQUEST_BATCH_SIZE`            | `10`    | Records evaluated against the repository per match-loop iteration.         |
| `FULFIL_BATCH_SIZE`             | `10`    | References requested per enhancement batch poll.                           |
| `REPOSITORY_LOOKUP_BATCH_SIZE`  | `100`   | Max identifiers per repository lookup call (1–100; IDs travel in the URL). |
| `REPOSITORY_LOOKUP_CONCURRENCY` | `1`     | Max concurrent lookup requests.                                            |

## Enhancement rules

These feed the plausibility checks described on the [Enhancement rules](matching.md) page:

| Variable                         | Default | Description                                                                                                                           |
| -------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `MIN_ABSTRACT_LENGTH`            | `200`   | Shorter abstracts are never considered (in either direction).                                                                         |
| `MAX_ABSTRACT_LENGTH`            | `10000` | Abstracts this long or longer are considered untrustworthy (in either direction): neither submitted nor treated as "already covered". |
| `MAX_TITLE_SLOP`                 | `10`    | Max normalised edit distance for titles to count as the same paper.                                                                   |
| `USE_SLOP_FOR_ENHANCEMENT_CHECK` | `false` | Off: any trustworthy repository abstract counts as "we already have it". On: abstracts are compared and only a _similar_ one counts.  |
| `MAX_ABSTRACT_SLOP`              | `100`   | Max normalised edit distance for abstracts to count as identical. Only used when `USE_SLOP_FOR_ENHANCEMENT_CHECK` is enabled.         |
| `PUBLICATION_YEAR_TOLERANCE`     | `2`     | Years are allowed to be this many years apart when both are known.                                                                    |

## Transfer (queue runner)

| Variable                          | Default                     | Description                                                                                                         |
| --------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `API_WRAPPER_ENDPOINT`            | `http://localhost:8000/api` | Public REST API of the Academic API Importer tool (used for progress reporting).                                    |
| `API_WRAPPER_API_KEY`             | `very-secret`               | Auth key for that API.                                                                                              |
| `API_WRAPPER_CHECKPOINT_INTERVAL` | `1000`                      | Rows per commit/checkpoint while transferring a file.                                                               |
| `TRANSFER_LOCKS_PATH`             | `.locks`                    | Directory holding per-ingest file locks. On a shared host this must be a filesystem all transfer processes can see. |

## Logging

Logging uses a standard Python `dictConfig` file, `.config/logging.json` by default (override with `LOG_CONFIG`). It silences chatty third-party loggers (`sqlalchemy`, `httpx`, `azure`, …); edit it to raise database or HTTP verbosity when debugging.

The root level is overridden per run by the `--loglevel` CLI option (defaults to `INFO`), or by `LOGLEVEL` when the runner initialises logging without an explicit level.

```sh
uv run python run_robot.py match --loglevel DEBUG
```
