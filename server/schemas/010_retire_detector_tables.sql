-- Task-registry Phase 4 (2026-09-19): the outreach detector probed
-- unanswered outreach goals; outreach is now steer+task with due
-- backstops, so the detector and its tables retire.
DROP TABLE IF EXISTS outreach_probe_log;
DROP TABLE IF EXISTS outreach_detector_watermark;
