CREATE TABLE IF NOT EXISTS monitoring
(
    timestamp timestamp,
    key       varchar,  -- key used for this metric
    value     float,    -- value of this metric at this time
    type      varchar   -- type of entry (orig | min | max | avg | count)
);

-- Number of ingest entries
INSERT INTO monitoring
SELECT now() as timestamp, 'ingest_entries' as key, count(1) as value, 'orig' as type
FROM search_ingest;
-- Number of ingest running ingests
INSERT INTO monitoring
SELECT now() as timestamp, 'ingest_entries_running' as key, count(1)as value, 'orig' as type
FROM search_ingest
WHERE is_queueing = TRUE
  AND is_queued <> FALSE;
-- Number of ingest finished ingests
INSERT INTO monitoring
SELECT now() as timestamp, 'ingest_entries_finished' as key, count(1)as value, 'orig' as type
FROM search_ingest
WHERE is_queued = TRUE;
-- Progress on individual ingestion (only if is_queued <> false)
INSERT INTO monitoring
SELECT now() as timestamp, 'ingest_entries_progress:' || ingest_id::text as key, checkpoint_rows as value, 'orig' as type
FROM search_ingest
WHERE is_queued = TRUE;


-- Number of records
INSERT INTO monitoring
SELECT now() as timestamp, 'records_total' as key, count(1)as value, 'orig' as type
FROM records;
-- Number of records where has_eligible_abstract=TRUE
INSERT INTO monitoring
SELECT now() as timestamp, 'records_has_eligible_abstract' as key, count(1) as value, 'orig' as type
FROM records
WHERE has_eligible_abstract = TRUE;
-- Number of records where evaluated=TRUE
INSERT INTO monitoring
SELECT now() as timestamp, 'records_evaluated' as key, count(1) as value, 'orig' as type
FROM records
WHERE evaluated = TRUE;
-- Number of records where found_destiny_reference=TRUE
INSERT INTO monitoring
SELECT now() as timestamp, 'records_found_destiny_reference' as key, count(1) as value, 'orig' as type
FROM records
WHERE found_destiny_reference = TRUE;
-- Number of records where abstract_enhancement_required=TRUE
INSERT INTO monitoring
SELECT now() as timestamp, 'records_abstract_enhancement_required' as key, count(1) as value, 'orig' as type
FROM records
WHERE abstract_enhancement_required = TRUE;
-- Number of records where enhancement_requested=TRUE
INSERT INTO monitoring
SELECT now() as timestamp, 'records_enhancement_requested' as key, count(1) as value, 'orig' as type
FROM records
WHERE enhancement_requested = TRUE;
-- Number of records where enhancement_submitted=TRUE
INSERT INTO monitoring
SELECT now() as timestamp, 'records_enhancement_submitted' as key, count(1) as value, 'orig' as type
FROM records
WHERE enhancement_submitted = TRUE;
-- Number of records where encountered_parsing_error=TRUE
INSERT INTO monitoring
SELECT now() as timestamp, 'records_encountered_parsing_error' as key, count(1) as value, 'orig' as type
FROM records
WHERE encountered_parsing_error = TRUE;
