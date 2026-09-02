-- Store attachment metadata as JSON (filenames, paths, content types).
ALTER TABLE email_messages ADD COLUMN attachments_json TEXT;
