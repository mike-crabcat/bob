-- Add plan and success_criteria fields to projects for self-executing project system
ALTER TABLE projects ADD COLUMN plan TEXT;  -- JSON array of plan steps
ALTER TABLE projects ADD COLUMN success_criteria TEXT;  -- JSON array of success criteria
ALTER TABLE projects ADD COLUMN subagent_session_key TEXT;  -- Subagent session key for active execution
ALTER TABLE projects ADD COLUMN auto_execute INTEGER DEFAULT 0;  -- Whether to auto-execute when active
