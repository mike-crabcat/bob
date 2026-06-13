CREATE TABLE IF NOT EXISTS persona_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

INSERT OR IGNORE INTO persona_config (key, value) VALUES
    ('owner_name', 'Mike'),
    ('model', 'OpenAI 5.4 mini'),
    ('channel', 'WhatsApp'),
    ('host', 'mike-workstation');
