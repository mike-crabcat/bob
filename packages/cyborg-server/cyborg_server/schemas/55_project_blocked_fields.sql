-- Add blocked fields to projects table for webhook notifications

ALTER TABLE projects ADD COLUMN blocked_reason TEXT;
ALTER TABLE projects ADD COLUMN blocked_resume_instructions TEXT;
ALTER TABLE projects ADD COLUMN metadata TEXT;
