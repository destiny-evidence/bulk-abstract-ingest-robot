-- Delete records that are older and already submitted or will never be submitted
DELETE
FROM records
WHERE (enhancement_submitted = TRUE OR abstract_enhancement_required = FALSE)
  AND created_at < NOW() - INTERVAL '14 days';