-- Add updated_at column to projects table if it does not already exist.
ALTER TABLE projects ADD COLUMN updated_at TEXT;
