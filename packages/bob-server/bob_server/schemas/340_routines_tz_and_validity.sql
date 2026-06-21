-- Routines: per-routine timezone and validity window
ALTER TABLE routines ADD COLUMN timezone TEXT;
ALTER TABLE routines ADD COLUMN valid_from TEXT;
ALTER TABLE routines ADD COLUMN valid_until TEXT;
