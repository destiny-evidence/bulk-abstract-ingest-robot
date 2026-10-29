SELECT
    date_trunc('minute', timestamp) as time,
    MAX(CASE WHEN key = 'records_total' THEN value END) AS records_total,
    MAX(CASE WHEN key = 'records_evaluated' THEN value END) AS records_evaluated,
    MAX(CASE WHEN key = 'records_has_eligible_abstract' THEN value END) AS records_has_eligible_abstract,
    MAX(CASE WHEN key = 'records_encountered_parsing_error' THEN value END) AS records_parsing_error,
    MAX(CASE WHEN key = 'records_enhancement_submitted' THEN value END) AS records_enhancement_submitted,
    MAX(CASE WHEN key = 'records_enhancement_requested' THEN value END) AS records_enhancement_requested,
    MAX(CASE WHEN key = 'records_abstract_enhancement_required' THEN value END) AS records_abstract_enhancement_required,
    MAX(CASE WHEN key = 'records_found_destiny_reference' THEN value END) AS records_found_destiny_reference,
    MAX(CASE WHEN key = 'ingest_entries' THEN value END) AS ingest_entries,
    MAX(CASE WHEN key = 'ingest_entries_running' THEN value END) AS ingest_entries_running,
    MAX(CASE WHEN key = 'ingest_entries_finished' THEN value END) AS ingest_entries_finished
FROM monitoring
GROUP BY date_trunc('minute', timestamp)
ORDER BY time DESC
LIMIT 500;