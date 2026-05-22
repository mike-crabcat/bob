-- Migrate legacy bobvoice: session keys to the standard agent:main:voice:session: format.
-- bobvoice:{rest} → agent:main:voice:session:{rest}

UPDATE session_messages
SET session_key = 'agent:main:voice:session:' || substr(session_key, 10)
WHERE session_key LIKE 'bobvoice:%';

UPDATE dispatches
SET session_key = 'agent:main:voice:session:' || substr(session_key, 10)
WHERE session_key LIKE 'bobvoice:%';

UPDATE session_summaries
SET session_key = 'agent:main:voice:session:' || substr(session_key, 10)
WHERE session_key LIKE 'bobvoice:%';
