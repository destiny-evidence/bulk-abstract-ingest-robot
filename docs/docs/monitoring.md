# Monitoring

The robot reports through its database — there is no metrics server and nothing is pushed anywhere. A small `sql-cron` service (in `compose.yaml`) runs SQL scripts from `scripts/` on a schedule: one collects counters into a `monitoring` table, one keeps that table (and the cache itself) from growing without bound, and one turns the stored counters into a readable snapshot.

```mermaid
flowchart LR
    subgraph cache["Postgres cache"]
        DATA[("records ·<br/>search_ingest")]
        MON[("monitoring<br/><i>timestamp · key ·<br/>value · type</i>")]
    end
    TRACK["scripts/tracking.sql<br/><i>sql-cron: every minute</i>"]
    COMPACT["scripts/compact.sql<br/><i>sql-cron: daily at 10:00</i>"]
    VIEW["scripts/monitor.sql<br/><i>run ad hoc</i>"]

    DATA -->|"count funnel flags"| TRACK
    TRACK -->|"append snapshot<br/>(type = orig)"| MON
    MON -->|"read aged-out rows"| COMPACT
    COMPACT -->|"rewrite as daily<br/>min/avg/max, prune"| MON
    COMPACT -->|"purge submitted /<br/>ineligible rows"| DATA
    MON -->|"pivot latest<br/>snapshots"| VIEW
```

## Collecting — `tracking.sql` (every minute)

The `monitoring` table is a plain key/value time series (`timestamp`, `key`, `value`, `type`) created on first run by the script itself — it is the only table not defined by SQLModel. Each run appends one snapshot of counters (as `orig` rows), so progress is observable as a trend instead of only in the log stream.

Currently tracked metrics:

**Transfer state** (counted over `search_ingest`):

| Metric                                | Counts                                                                  |
| ------------------------------------- | ----------------------------------------------------------------------- |
| `ingest_entries`                      | transfer requests in total                                              |
| `ingest_entries_running`              | transfers claimed (`is_queueing`) but not finished                      |
| `ingest_entries_finished`             | transfers completed (`is_queued`)                                       |
| `ingest_entries_progress:<ingest_id>` | final size (`checkpoint_rows`) of one transfer — one series per request |

**Record funnel** (counted over `records`, one metric per tracking flag — see [Data model → Tracking columns](data-model.md#tracking-columns)):

| Metric                                  | Counts                                                 |
| --------------------------------------- | ------------------------------------------------------ |
| `records_total`                         | rows in the cache                                      |
| `records_has_eligible_abstract`         | rows whose abstract passed the length pre-filter       |
| `records_evaluated`                     | rows checked against the repository                    |
| `records_found_destiny_reference`       | rows matched to a DESTinY reference                    |
| `records_abstract_enhancement_required` | rows the [enhancement rules](matching.md) accepted     |
| `records_enhancement_requested`         | rows an enhancement request was sent for               |
| `records_enhancement_submitted`         | rows whose enhancement actually reached the repository |
| `records_encountered_parsing_error`     | rows whose raw payload could not be parsed             |

To add a metric, append an `INSERT INTO monitoring SELECT now(), '<key>', count(...), 'orig' FROM ...` statement to `tracking.sql` — and add a matching `CASE WHEN` column to `monitor.sql`, whose pivot columns are hardcoded.

## Compacting — `compact.sql` (daily at 10:00)

Three housekeeping passes in one script:

1. **Aggregate aged-out metrics.** Raw (`orig`) rows older than seven days are collapsed into one daily `min` / `avg` / `max` row per metric key, and the raw rows are dropped.
2. **Cap the table size.** Only the 10,000 most recent `monitoring` rows survive each run.
3. **Purge cache rows that no longer matter.** Submitted or permanently ineligible records older than 14 days are deleted from `records`, so the cache does not grow forever while the raw source data stays in (cold) blob storage anyway and can be re-ingested via a new `search_ingest` row.

<!-- prettier-ignore -->
!!! warning "The purge can outrun the match runner"
    `abstract_enhancement_required` defaults to `false` — so records that were _never evaluated_ (e.g. the robot was down while a large transfer ran) are also deleted once they are 14 days old, and will never be retried. Lengthen the interval if your batches may lag that far behind.

## Viewing — `monitor.sql` (ad hoc)

`monitor.sql` pivots the newest `monitoring` snapshots into a wide table — one row per minute, one column per metric, newest first (last 500 rows):

```sh
psql postgresql://pguser:password@127.0.0.1:5432/references -f scripts/monitor.sql
```

<!-- prettier-ignore -->
!!! info "Optional Evidence dashboard"
    An OpenTelemetry setup exists in the Docker image (`opentelemetry-*` deps) but is not configured end to end.
