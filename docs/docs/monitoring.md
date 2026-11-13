# Monitoring

The robot reports through its database — there is no metrics server and nothing is pushed anywhere. A small `sql-cron` service (in `compose.yaml`) runs SQL scripts from `scripts/` on a schedule: one collects counters into a `monitoring` table, one keeps that table (and the cache itself) from growing without bound, and one turns the stored counters into a readable snapshot.

```mermaid
flowchart LR
    subgraph cache["Postgres cache"]
        DATA[("records ·<br/>m2m_record_ods ·<br/>search_ingest")]
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

**Record funnel** (counted over `records` — see [Data model → Tracking columns](data-model.md#tracking-columns)):

| Metric                              | Counts                                                    |
| ----------------------------------- | --------------------------------------------------------- |
| `records_total`                     | rows in the cache                                         |
| `records_has_eligible_abstract`     | rows whose abstract passed the length pre-filter          |
| `records_evaluated`                 | rows checked against the repository                       |
| `records_encountered_parsing_error` | rows parked because their raw payload could not be parsed |

**Match funnel** (counted over `m2m_record_ods`, one row per record/reference pair — see [Data model → `m2m_record_ods`](data-model.md#m2m_record_ods-the-matched-references)):

| Metric                                  | Counts                                                  |
| --------------------------------------- | ------------------------------------------------------- |
| `matches_total`                         | references the cached records were matched to           |
| `matches_abstract_enhancement_required` | pairs the [enhancement rules](matching.md) accepted     |
| `matches_enhancement_requested`         | pairs an enhancement request was sent for               |
| `matches_enhancement_submitted`         | pairs whose enhancement actually reached the repository |

Because one record can match several references, the match counters intentionally run ahead of the record counters: `matches_total / records_evaluated` is roughly how many repository hits an average record gets.

<!-- prettier-ignore -->
!!! tip "Reading required-but-unrequested pairs"
    `matches_abstract_enhancement_required − matches_enhancement_requested` is the number of accepted pairs whose enhancement request has not gone out. A momentary gap is normal queueing; a gap that persists is a failing request API: when the repository is unreachable, the match runner logs it (`Enhancement request for … failed`) and records the batch anyway, leaving those pairs `required` without `requested`. The runner does not retry them by itself — re-request them (or sweep for that flag combination) once the repository answers again, before the 14-day purge [takes the rows](#compacting-compactsql-daily-at-1000).

<!-- prettier-ignore -->
!!! tip "Reading the parsing-error counter"
    The robot keeps working when it hits an unparsable payload, so this metric is the only thing that surfaces a parser gap: any growth means rows are being parked, and the reason is in the match runner's log (`Failed record parsing: …` / `Failed record validation: …`). Fix the parser and clear the flag before the 14-day purge removes those rows — see [Data model → Parser fixes](data-model.md#parser-fixes).

To add a metric, append an `INSERT INTO monitoring SELECT now(), '<key>', count(...), 'orig' FROM ...` statement to `tracking.sql` — and add a matching `CASE WHEN` column to `monitor.sql`, whose pivot columns are hardcoded.

## Compacting — `compact.sql` (daily at 10:00)

Three housekeeping passes in one script:

1. **Aggregate aged-out metrics.** Raw (`orig`) rows older than seven days are collapsed into one daily `min` / `avg` / `max` row per metric key, and the raw rows are dropped.
2. **Cap the table size.** Only the 10,000 most recent `monitoring` rows survive each run.
3. **Purge cache rows that no longer matter.** Records older than 14 days that are not parked and have no match still awaiting its enhancement (required and not yet submitted) are deleted from `records`; their `m2m_record_ods` rows follow via `ON DELETE CASCADE`. The cache therefore does not grow forever while the raw source data stays in (cold) blob storage anyway and can be re-ingested via a new `search_ingest` row.

<!-- prettier-ignore -->
!!! warning "The purge can outrun the match runner"
    A record _without_ match rows counts as settled — so records that were _never evaluated_ (e.g. the robot was down while a large transfer ran) are also deleted once they are 14 days old, and will never be retried. Lengthen the interval if your batches may lag that far behind.

## Viewing — `monitor.sql` (ad hoc)

`monitor.sql` pivots the newest `monitoring` snapshots into a wide table — one row per minute, one column per metric, newest first (last 500 rows):

```sh
psql postgresql://pguser:password@127.0.0.1:5432/references -f scripts/monitor.sql
```

<!-- prettier-ignore -->
!!! info "Optional Evidence dashboard"
    An OpenTelemetry setup exists in the Docker image (`opentelemetry-*` deps) but is not configured end to end.
