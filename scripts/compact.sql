-- Compacts all records of a day into aggregates to save space
WITH
    aggregated AS (
        SELECT date_trunc('day', timestamp) AS timestamp,
               key,
               min(value)                   AS min_val,
               avg(value)                   AS avg_val,
               max(value)                   AS max_val
        FROM monitoring
        WHERE timestamp < now() - INTERVAL '7 days'
          AND type = 'orig'
        GROUP BY date_trunc('day', timestamp), key)
SELECT timestamp, key, 'min' AS metric_type, min_val AS metric
FROM aggregated
UNION ALL
SELECT timestamp, key, 'avg' AS metric_type, avg_val AS metric
FROM aggregated
UNION ALL
SELECT timestamp, key, 'max' AS metric_type, max_val AS metric
FROM aggregated
ORDER BY timestamp, key, metric_type;

-- Drop the now deprecated raw data from monitoring table
DELETE
FROM monitoring
WHERE timestamp < now() - INTERVAL '7 days'
  AND type = 'orig';

-- Delete records that are older and already submitted or will never be submitted
DELETE
FROM records
WHERE (enhancement_submitted = TRUE OR abstract_enhancement_required = FALSE)
  AND created_at < NOW() - INTERVAL '14 days';
