-- Session-model cleanup Increment 5: retire voice_session_messages.
--
-- The table served the LEGACY local STT->TTS pipeline (bobvoice:* language
-- practice sessions, last write 2026-05). Zero code references remain; the
-- realtime bridge writes transcripts to session_messages directly. Its 206
-- rows are NOT duplicated elsewhere, so archive them into session_messages
-- (provenance='legacy_voice') before dropping. Forward migration only —
-- historical schema files stay untouched for fresh-install ordering.

INSERT INTO session_messages (session_key, role, content, channel, created_at, dispatched, provenance)
SELECT session_key, role, text, 'voice', created_at, 1, 'legacy_voice'
FROM voice_session_messages v
WHERE NOT EXISTS (
    SELECT 1 FROM session_messages m
    WHERE m.session_key = v.session_key AND m.role = v.role
      AND m.content = v.text AND m.provenance = 'legacy_voice');

DROP TABLE IF EXISTS voice_session_messages;
