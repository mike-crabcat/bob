-- 007: MCP client support (2026-09-14).
-- Mike-registered external tool servers (stdio or streamable HTTP), exposed
-- to the LLM as native tools namespaced mcp_<server>_<tool>. Servers are
-- global or attached per conversation via conversation_mcp_attachments.
-- Registration is dashboard-API-only (token-gated writes, redacted GETs);
-- the agent gets tools, never registration powers.
CREATE TABLE IF NOT EXISTS mcp_servers (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,
  transport        TEXT NOT NULL CHECK (transport IN ('stdio','http')),
  command          TEXT NOT NULL DEFAULT '',
  args_json        TEXT NOT NULL DEFAULT '[]',
  env_json         TEXT NOT NULL DEFAULT '{}',
  url              TEXT NOT NULL DEFAULT '',
  headers_json     TEXT NOT NULL DEFAULT '{}',
  enabled          INTEGER NOT NULL DEFAULT 1,
  is_global        INTEGER NOT NULL DEFAULT 0,
  trusted_only     INTEGER NOT NULL DEFAULT 0,
  tool_filter_json TEXT NOT NULL DEFAULT '{}',
  timeout_seconds  INTEGER,
  note             TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_mcp_attachments (
  conversation_id TEXT NOT NULL,
  mcp_server_id   TEXT NOT NULL,
  attached_by     TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  PRIMARY KEY (conversation_id, mcp_server_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_attachments_server
  ON conversation_mcp_attachments(mcp_server_id);
