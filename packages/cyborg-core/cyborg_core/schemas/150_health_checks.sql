-- Project health checks for monitoring project status and risks
-- Records periodic health assessments and recommendations

CREATE TABLE IF NOT EXISTS project_health_checks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,

    -- Check type and origin
    check_type TEXT NOT NULL CHECK(check_type IN ('schedule', 'anomaly', 'milestone', 'manual', 'triggered')),

    -- Health assessment
    health_score REAL CHECK(health_score >= 0 AND health_score <= 1),
    risk_level TEXT CHECK(risk_level IN ('low', 'medium', 'high', 'critical')),

    -- Detailed indicators (JSON for flexibility)
    -- Examples: {"blocked_tasks": 2, "failed_tasks": 1, "overdue_days": 5}
    indicators JSON,

    -- Alert state
    alert_triggered BOOLEAN NOT NULL DEFAULT 0,
    alert_sent_at TEXT,

    -- Recommendations (JSON array)
    -- Examples: [{"priority": "high", "action": "...", "reason": "..."}]
    recommendations JSON,

    -- Additional metadata
    metadata JSON,

    -- Timestamps
    created_at TEXT NOT NULL DEFAULT (datetime('now')),

    -- Relationships
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_health_checks_project ON project_health_checks(project_id);
CREATE INDEX IF NOT EXISTS idx_health_checks_risk ON project_health_checks(risk_level);
CREATE INDEX IF NOT EXISTS idx_health_checks_created ON project_health_checks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_checks_alert ON project_health_checks(alert_triggered, risk_level);

-- Latest health check per project
CREATE VIEW IF NOT EXISTS latest_project_health AS
SELECT
    project_id,
    check_type,
    health_score,
    risk_level,
    indicators,
    alert_triggered,
    recommendations,
    created_at
FROM project_health_checks h1
WHERE created_at = (
    SELECT MAX(created_at)
    FROM project_health_checks h2
    WHERE h2.project_id = h1.project_id
);

-- Projects needing attention (alert triggered or high/critical risk)
CREATE VIEW IF NOT EXISTS projects_need_attention AS
SELECT
    p.id as project_id,
    p.title,
    p.state,
    h.check_type,
    h.health_score,
    h.risk_level,
    h.alert_triggered,
    h.recommendations,
    h.created_at as last_check_at,
    CASE
        WHEN h.risk_level IN ('high', 'critical') THEN 1
        WHEN h.alert_triggered THEN 1
        ELSE 0
    END as needs_attention
FROM projects p
INNER JOIN project_health_checks h ON h.project_id = p.id
WHERE h.created_at = (
    SELECT MAX(created_at)
    FROM project_health_checks
    WHERE project_id = p.id
)
AND (h.risk_level IN ('high', 'critical') OR h.alert_triggered = 1)
AND p.deleted_at IS NULL
AND p.state != 'closed'
ORDER BY h.risk_level DESC, h.created_at DESC;
