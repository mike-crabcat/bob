-- Normalize memory_bulletins timestamps to canonical UTC ISO 8601:
--   'YYYY-MM-DDTHH:MM:SSZ'
-- Previously created_at / session_range_start / session_range_end stored a mix
-- of formats ('2026-06-13 12:09:55', '2026-06-13T05:02:21.542886+00:00', etc.),
-- which broke lexical ORDER BY because 'T' (0x54) sorts after ' ' (0x20).
-- Strips fractional seconds and timezone offsets by truncating to the second.
-- SQLite's datetime() handles both 'T' and ' ' separators and ignores trailing
-- timezone metadata, so we can reformat every column uniformly.

UPDATE memory_bulletins
   SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', datetime(created_at))
 WHERE created_at != '' AND created_at IS NOT NULL;

UPDATE memory_bulletins
   SET session_range_start = strftime('%Y-%m-%dT%H:%M:%SZ', datetime(session_range_start))
 WHERE session_range_start != '' AND session_range_start IS NOT NULL;

UPDATE memory_bulletins
   SET session_range_end = strftime('%Y-%m-%dT%H:%M:%SZ', datetime(session_range_end))
 WHERE session_range_end != '' AND session_range_end IS NOT NULL;
