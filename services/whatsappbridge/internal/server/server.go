package server

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"go.cyborg.dev/whatsappbridge/internal/wsproto"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

type MessageHandler func(env wsproto.Envelope)

type Server struct {
	cfg     serverConfig
	log     *slog.Logger
	handler MessageHandler

	mu            sync.RWMutex
	client        *clientConn
	started       time.Time
	extraHandlers map[string]http.HandlerFunc
}

type serverConfig struct {
	listenAddr string
	token      string
}

type clientConn struct {
	conn *websocket.Conn
	send chan []byte
}

func New(listenAddr, token string, log *slog.Logger, handler MessageHandler) *Server {
	return &Server{
		cfg: serverConfig{
			listenAddr: listenAddr,
			token:      token,
		},
		log:     log,
		handler: handler,
	}
}

func (s *Server) RegisterHandler(path string, handler http.HandlerFunc) {
	if s.extraHandlers == nil {
		s.extraHandlers = make(map[string]http.HandlerFunc)
	}
	s.extraHandlers[path] = handler
}

func (s *Server) Start(ctx context.Context) error {
	s.started = time.Now()

	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/ws", s.handleWS)
	for path, handler := range s.extraHandlers {
		mux.HandleFunc(path, handler)
	}

	srv := &http.Server{Addr: s.cfg.listenAddr, Handler: mux}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(shutdownCtx)
		s.kickClient()
	}()

	s.log.Info("server listening", "addr", s.cfg.listenAddr)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return nil
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	waConnected := s.client != nil
	s.mu.RUnlock()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"status":             "ok",
		"whatsapp_connected": waConnected,
		"uptime_seconds":     int64(time.Since(s.started).Seconds()),
	})
}

func (s *Server) handleWS(w http.ResponseWriter, r *http.Request) {
	token := r.URL.Query().Get("token")
	if token != s.cfg.token {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		s.log.Error("ws upgrade failed", "error", err)
		return
	}

	s.registerClient(conn)
}

func (s *Server) registerClient(conn *websocket.Conn) {
	s.mu.Lock()
	// Kick existing client — newest wins
	if s.client != nil {
		close(s.client.send)
		s.client.conn.Close()
	}
	c := &clientConn{conn: conn, send: make(chan []byte, 256)}
	s.client = c
	s.mu.Unlock()

	s.log.Info("client connected")

	// Read pump
	go s.readPump(c)
	// Write pump
	go s.writePump(c)
}

func (s *Server) kickClient() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.client != nil {
		close(s.client.send)
		s.client.conn.Close()
		s.client = nil
	}
}

func (s *Server) readPump(c *clientConn) {
	defer func() {
		s.mu.Lock()
		if s.client == c {
			s.client = nil
		}
		s.mu.Unlock()
		c.conn.Close()
		s.log.Info("client disconnected")
	}()

	c.conn.SetReadLimit(1 << 20) // 1MB
	c.conn.SetReadDeadline(time.Now().Add(60 * time.Second))
	c.conn.SetPongHandler(func(string) error {
		c.conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		return nil
	})

	for {
		_, msg, err := c.conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseNormalClosure) {
				s.log.Warn("read error", "error", err)
			}
			return
		}
		env, err := wsproto.ParseEnvelope(msg)
		if err != nil {
			s.log.Warn("invalid message", "error", err)
			continue
		}
		if s.handler != nil {
			s.handler(env)
		}
	}
}

func (s *Server) writePump(c *clientConn) {
	ticker := time.NewTicker(30 * time.Second)
	defer func() {
		ticker.Stop()
		c.conn.Close()
	}()

	for {
		select {
		case msg, ok := <-c.send:
			c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if !ok {
				c.conn.WriteMessage(websocket.CloseMessage, []byte{})
				return
			}
			if err := c.conn.WriteMessage(websocket.TextMessage, msg); err != nil {
				return
			}
		case <-ticker.C:
			c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

// Send sends a message to the connected client. Returns false if no client is connected.
func (s *Server) Send(env wsproto.Envelope) bool {
	data, err := env.Marshal()
	if err != nil {
		s.log.Error("marshal message", "error", err)
		return false
	}

	s.mu.RLock()
	c := s.client
	s.mu.RUnlock()

	if c == nil {
		return false
	}

	select {
	case c.send <- data:
		return true
	default:
		s.log.Warn("client send buffer full, dropping message", "type", env.Type)
		return false
	}
}

// HasClient returns true if a cyborg-server client is connected.
func (s *Server) HasClient() bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.client != nil
}

// ClientCount returns the number of connected clients (0 or 1).
func (s *Server) ClientCount() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.client != nil {
		return 1
	}
	return 0
}

// Uptime returns seconds since the server started.
func (s *Server) Uptime() int64 {
	return int64(time.Since(s.started).Seconds())
}
