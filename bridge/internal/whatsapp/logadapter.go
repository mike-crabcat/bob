package whatsapp

import (
	"fmt"
	"log/slog"

	waLog "go.mau.fi/whatsmeow/util/log"
)

func sprintf(msg string, args []any) string {
	if len(args) == 0 {
		return msg
	}
	return fmt.Sprintf(msg, args...)
}

// slogLogger adapts whatsmeow's waLog.Logger onto slog, so the library's
// internal diagnostics (websocket stream errors, pairing handshake
// failures, reconnect attempts) reach the bridge's normal log output.
//
// Before this existed the client was built with a nil logger — during the
// 2026-09-09 pairing outage every connect/pair failure was invisible: the
// bridge showed a healthy socket while the phone reported rejected codes,
// and nothing explained why.
type slogLogger struct {
	l *slog.Logger
}

// NewSlogLogger wraps a *slog.Logger as a waLog.Logger.
func NewSlogLogger(l *slog.Logger) waLog.Logger {
	return &slogLogger{l: l}
}

func (s *slogLogger) Errorf(msg string, args ...any) { s.l.Error(sprintf(msg, args)) }
func (s *slogLogger) Warnf(msg string, args ...any)  { s.l.Warn(sprintf(msg, args)) }
func (s *slogLogger) Infof(msg string, args ...any)  { s.l.Info(sprintf(msg, args)) }
func (s *slogLogger) Debugf(msg string, args ...any) { s.l.Debug(sprintf(msg, args)) }

func (s *slogLogger) Sub(module string) waLog.Logger {
	return &slogLogger{l: s.l.With("module", module)}
}
