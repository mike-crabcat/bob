-- Migrate existing voice_session_messages into session_messages.

INSERT OR IGNORE INTO session_messages (id, session_key, role, content, channel, created_at)
    SELECT
        'vsm-' || id,
        session_key,
        role,
        text,
        'voice',
        created_at
    FROM voice_session_messages;
