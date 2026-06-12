-- Migration: add metadata to calendars for routing and session linkage.

ALTER TABLE calendars ADD COLUMN metadata TEXT;
