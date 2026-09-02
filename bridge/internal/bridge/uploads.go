package bridge

import (
	"sync"
	"time"

	"github.com/google/uuid"
)

// UploadEntry holds the bytes and metadata for a single media upload.
type UploadEntry struct {
	Data      []byte
	MimeType  string
	FileName  string
	ExpiresAt time.Time
}

// UploadStore is an in-memory, single-use store for media uploads.
// Entries are deleted on first read (Take) and evicted by Sweep after TTL.
type UploadStore struct {
	mu       sync.Mutex
	entries  map[string]*UploadEntry
	ttl      time.Duration
	maxBytes int64
}

func NewUploadStore(ttl time.Duration, maxBytes int64) *UploadStore {
	return &UploadStore{
		entries:  make(map[string]*UploadEntry),
		ttl:      ttl,
		maxBytes: maxBytes,
	}
}

// Put stores the upload and returns its ID.
func (s *UploadStore) Put(data []byte, mime, filename string) string {
	id := uuid.New().String()
	entry := &UploadEntry{
		Data:      data,
		MimeType:  mime,
		FileName:  filename,
		ExpiresAt: time.Now().Add(s.ttl),
	}
	s.mu.Lock()
	s.entries[id] = entry
	s.mu.Unlock()
	return id
}

// Take removes and returns the entry. Single-use: a second Take for the same ID returns false.
func (s *UploadStore) Take(id string) (*UploadEntry, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	entry, ok := s.entries[id]
	if !ok {
		return nil, false
	}
	delete(s.entries, id)
	return entry, true
}

// Sweep deletes expired entries. Called periodically by the bridge.
func (s *UploadStore) Sweep() {
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	for id, entry := range s.entries {
		if now.After(entry.ExpiresAt) {
			delete(s.entries, id)
		}
	}
}
