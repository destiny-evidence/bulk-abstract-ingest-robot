# Development & operations

## Prerequisites

- [uv](https://docs.astral.sh/uv) for dependency and virtual-environment management
- Docker (for the Compose stack with Postgres)
- Access to a DESTinY repository deployment — locally or a registered robot on staging/production

## Local setup

Install dependencies and activate the environment:

```sh
uv sync
source .venv/bin/activate
```

Create an environment file (`.config/.env.example` is a starting point — see [Configuration](configuration.md) for the full list):

```sh
cp .config/.env.example .env
```

At minimum you need `ROBOT_ID`, `ROBOT_SECRET` and the Keycloak client credentials. If you run against a local repository deployment that doesn't enforce authentication you can use dummy credentials, but the robot will still try to sign requests, so point it at a deployment that ignores signatures.

### Database

For local development, Postgres runs as part of the Docker Compose stack. Tables are **not** created by SQL scripts — `init.sql` is a placeholder; the schema is created automatically by SQLModel (`SQLModel.metadata.create_all`) when a runner starts.

Useful commands:

```sh
docker compose up -d postgres
docker compose logs -f postgres
psql postgresql://pguser:password@127.0.0.1:5432/references   # the port is published to the host
docker compose down    # add -v to drop the database volume
```

To point the robot at a different database (e.g. the VM below), set `DB_HOST` in your `.env` file.

## Running the robot

Run a single runner:

```sh
uv run python run_robot.py match      # evaluate cached records against the repository
uv run python run_robot.py enhance    # fulfil approved enhancement batches
uv run python run_robot.py queue      # stream export files from blob storage into the cache
```

Run the whole Compose stack (database, all three runners, plus monitoring helpers):

```sh
docker compose up --build
```

Services in `compose.yaml`:

| Service      | Runs                                                                                                          |
| ------------ | ------------------------------------------------------------------------------------------------------------- |
| `postgres`   | Postgres 18, volume-persisted, port published to `127.0.0.1:5432`                                             |
| `match`      | `python run_robot.py match` with `DB_HOST=postgres`                                                           |
| `enhance`    | `python run_robot.py enhance` with `DB_HOST=postgres`                                                         |
| `queue`      | `python run_robot.py queue` with `DB_HOST=postgres` and the shared locks volume mounted at `/app/.data/locks` |
| `locks-init` | one-shot: creates the host locks directory with the right ownership before `queue` starts                     |
| `sql-cron`   | runs `scripts/tracking.sql` every minute and `scripts/compact.sql` daily (see [Monitoring](monitoring.md))    |

```sh
docker compose logs -f match
docker compose logs -f enhance
docker compose logs -f queue
docker compose down
```

### Feeding data manually

A transfer request can be inserted directly:

```sql
INSERT INTO search_ingest (azure_link) VALUES ('https://<storage>.blob.core.windows.net/<container>/<file>.jsonl?<SAS-token>');
```

…or by calling `IngestTracker.add_ingest(azure_link=...)`. Progress can be watched via:

```sql
SELECT ingest_id, is_queueing, is_queued, checkpoint_bytes, checkpoint_rows FROM search_ingest;
SELECT evaluated, found_destiny_reference, abstract_enhancement_required,
       enhancement_requested, enhancement_submitted, count(*)
FROM records GROUP BY 1,2,3,4,5 ORDER BY 6 DESC;
```

## Authentication against the DESTinY repository

The robot talks to the repository through two `destiny_sdk` clients (set up in `app/util/repository.py`):

- **`RobotClient`** — HMAC-signed calls (request signature with `ROBOT_SECRET`) for polling enhancement batches and submitting batch results.
- **`OAuthClient` + Keycloak** — client-credentials OAuth for reference lookups and enhancement requests.

To run against a real deployment, register the robot following [the ODS robot-registration procedure](https://destiny-evidence.github.io/destiny-repository/procedures/robot-registration.html); registration yields the `ROBOT_ID` and `ROBOT_SECRET`. Keycloak client credentials are provisioned separately by the deployment.

## Code quality

Before committing, run the pre-commit hooks (formatting, linting, type checks) to keep diffs minimal:

```sh
uv run pre-commit install             # once
uv run pre-commit run --all-files     # manually
```

Hooks are configured in `.pre-commit-config.yaml`.

## Documentation

The docs site lives in `docs/` and is built with [Zensical](https://zensical.org) (Material-compatible). Sources are in `docs/docs/`, configuration in `docs/zensical.toml`.

```sh
uv run zensical serve    # live preview at http://127.0.0.1:8000
uv run zensical build --clean
```

Mermaid diagrams are written as ` ```mermaid ` code blocks directly in the Markdown.

## Operations notes

- **Restart safety:** every runner can be killed and restarted at any time — see [Architecture → Crash resilience](architecture.md#crash-resilience).
- **Cleanup:** automatic — `scripts/compact.sql` (via `sql-cron`) purges submitted or ineligible records older than 14 days, see [Monitoring → Compacting](monitoring.md#compacting-compactsql-daily-at-1000).
- **Monitoring:** metric collection, compaction and the snapshot query are described on the [Monitoring](monitoring.md) page.
- **Deployment:** current deployment runs the Compose stack (or bare processes) on a PIK VM with a hosted Postgres. `infra/` contains Azure Container App Terraform scaffolding carried over from the toy-robot template — its variable names predate the current `Settings` and need updating before use.
