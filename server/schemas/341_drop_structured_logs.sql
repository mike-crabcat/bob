-- Drop the structured_logs table.
--
-- This table was written by DatabaseLogHandler in structured_logging.py, but
-- that handler was broken from inception (asyncio task was never tracked,
-- writes silently dropped) and was removed. Nothing queried the table.
-- Removing the schema to match the removal of the code that produced it.

DROP TABLE IF EXISTS structured_logs;
