-- Linkage columns for openai_voice subagents.
--
-- `subagents.contact_id` / `subagents.modality` carry the dispatch inputs for
-- an openai_voice subagent (which contact to call, and via which audio path).
-- `voice_sessions.subagent_id` lets VoiceSessionService.complete mark the
-- originating subagent completed when the browser voice_link call ends.

ALTER TABLE voice_sessions ADD COLUMN subagent_id TEXT;
ALTER TABLE subagents ADD COLUMN contact_id TEXT;
ALTER TABLE subagents ADD COLUMN modality TEXT NOT NULL DEFAULT '';
