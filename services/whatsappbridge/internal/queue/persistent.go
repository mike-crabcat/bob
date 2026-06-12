package queue

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "github.com/mattn/go-sqlite3"

	"go.bob.dev/whatsappbridge/internal/wsproto"
)

const schema = `
CREATE TABLE IF NOT EXISTS queue_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT NOT NULL CHECK(queue IN ('incoming', 'outgoing')),
    message_id TEXT NOT NULL UNIQUE,
    message_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'in_flight', 'delivered', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_queue_pending ON queue_messages(queue, status, created_at);
`

type Stream string

const (
	Incoming Stream = "incoming"
	Outgoing Stream = "outgoing"
)

type PersistentQueue struct {
	db  *sql.DB
	ttl time.Duration
}

func New(dbPath string, ttl time.Duration) (*PersistentQueue, error) {
	db, err := sql.Open("sqlite3", dbPath+"?_journal_mode=WAL&_busy_timeout=5000")
	if err != nil {
		return nil, fmt.Errorf("open queue db: %w", err)
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("create queue schema: %w", err)
	}
	return &PersistentQueue{db: db, ttl: ttl}, nil
}

func (q *PersistentQueue) Close() error {
	return q.db.Close()
}

// Enqueue writes a message to the queue. Returns error on duplicate message_id.
func (q *PersistentQueue) Enqueue(stream Stream, messageID, msgType string, payload any) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal payload: %w", err)
	}
	_, err = q.db.Exec(
		`INSERT INTO queue_messages (queue, message_id, message_type, payload) VALUES (?, ?, ?, ?)`,
		stream, messageID, msgType, string(raw),
	)
	return err
}

// DrainPending returns all pending or in-flight messages for a stream, ordered by creation time.
// Marks them as in_flight.
func (q *PersistentQueue) DrainPending(stream Stream) ([]QueuedMessage, error) {
	rows, err := q.db.Query(
		`SELECT id, message_id, message_type, payload, created_at, attempts
		 FROM queue_messages
		 WHERE queue = ? AND status IN ('pending', 'in_flight')
		 ORDER BY created_at ASC`,
		stream,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var msgs []QueuedMessage
	for rows.Next() {
		var m QueuedMessage
		if err := rows.Scan(&m.RowID, &m.MessageID, &m.MessageType, &m.PayloadJSON, &m.CreatedAt, &m.Attempts); err != nil {
			return nil, err
		}
		msgs = append(msgs, m)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// Mark as in_flight
	if len(msgs) > 0 {
		_, err = q.db.Exec(
			`UPDATE queue_messages SET status = 'in_flight', last_attempt_at = datetime('now'), attempts = attempts + 1
			 WHERE queue = ? AND status IN ('pending', 'in_flight')`,
			stream,
		)
	}
	return msgs, err
}

// MarkDelivered marks a message as delivered.
func (q *PersistentQueue) MarkDelivered(messageID string) error {
	_, err := q.db.Exec(
		`UPDATE queue_messages SET status = 'delivered' WHERE message_id = ?`,
		messageID,
	)
	return err
}

// MarkFailed marks a message as failed.
func (q *PersistentQueue) MarkFailed(messageID string) error {
	_, err := q.db.Exec(
		`UPDATE queue_messages SET status = 'failed' WHERE message_id = ?`,
		messageID,
	)
	return err
}

// MarkPending resets an in-flight message back to pending (e.g., on reconnection).
func (q *PersistentQueue) MarkPending(messageID string) error {
	_, err := q.db.Exec(
		`UPDATE queue_messages SET status = 'pending' WHERE message_id = ? AND status = 'in_flight'`,
		messageID,
	)
	return err
}

// PendingCount returns the number of pending messages for a stream.
func (q *PersistentQueue) PendingCount(stream Stream) (int, error) {
	var count int
	err := q.db.QueryRow(
		`SELECT COUNT(*) FROM queue_messages WHERE queue = ? AND status IN ('pending', 'in_flight')`,
		stream,
	).Scan(&count)
	return count, err
}

// EnqueueAndBuild is a convenience that creates an envelope and enqueues the payload.
func (q *PersistentQueue) EnqueueAndBuild(stream Stream, messageID string, msgType string, payload any) (wsproto.Envelope, error) {
	env := wsproto.NewEnvelope(msgType, payload)
	if err := q.Enqueue(stream, messageID, msgType, payload); err != nil {
		return env, err
	}
	return env, nil
}

// Cleanup removes delivered and expired messages.
func (q *PersistentQueue) Cleanup() error {
	cutoff := time.Now().UTC().Add(-q.ttl).Format(time.RFC3339)
	_, err := q.db.Exec(
		`DELETE FROM queue_messages
		 WHERE status = 'delivered'
		    OR (status = 'failed' AND created_at < ?)
		    OR (created_at < ? AND status NOT IN ('pending', 'in_flight'))`,
		cutoff, cutoff,
	)
	return err
}

type QueuedMessage struct {
	RowID       int64
	MessageID   string
	MessageType string
	PayloadJSON string
	CreatedAt   string
	Attempts    int
}

// Envelope converts a queued message into a WebSocket envelope.
func (m *QueuedMessage) Envelope() (wsproto.Envelope, error) {
	return wsproto.Envelope{
		Type:      m.MessageType,
		ID:        m.MessageID,
		Timestamp: m.CreatedAt,
		Payload:   json.RawMessage(m.PayloadJSON),
	}, nil
}
