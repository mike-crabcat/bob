package bridge

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.bob.dev/whatsappbridge/internal/config"
	"go.bob.dev/whatsappbridge/internal/queue"
	"go.bob.dev/whatsappbridge/internal/server"
	whatsapp "go.bob.dev/whatsappbridge/internal/whatsapp"
	"go.bob.dev/whatsappbridge/internal/wsproto"
)

type Bridge struct {
	cfg  *config.Config
	log  *slog.Logger
	srv  *server.Server
	wa   *whatsapp.Client
	inQ  *queue.PersistentQueue
	outQ *queue.PersistentQueue

	pairMu   sync.RWMutex
	lastQR   string
	lastPair string

	uploads *UploadStore
}

func New(cfg *config.Config, log *slog.Logger) (*Bridge, error) {
	inQ, err := queue.New(cfg.QueueDBPath(), cfg.IncomingQueueTTL)
	if err != nil {
		return nil, err
	}

	outQ, err := queue.New(cfg.QueueDBPath(), cfg.IncomingQueueTTL)
	if err != nil {
		inQ.Close()
		return nil, err
	}

	wa, err := whatsapp.NewClient(cfg.SessionDBPath(), cfg.DataDir, log.With("component", "whatsapp"))
	if err != nil {
		inQ.Close()
		outQ.Close()
		return nil, err
	}

	b := &Bridge{
		cfg:     cfg,
		log:     log.With("component", "bridge"),
		srv:     server.New(cfg.ListenAddr(), cfg.Token, log.With("component", "server"), nil),
		wa:      wa,
		inQ:     inQ,
		outQ:    outQ,
		uploads: NewUploadStore(5*time.Minute, 100<<20),
	}

	// Wire event handlers
	b.srv = server.New(cfg.ListenAddr(), cfg.Token, log.With("component", "server"), b.handleClientMessage)
	b.srv.OnConnect(func() {
		b.log.Info("bob client connected")
		b.drainIncoming()
		if b.wa.IsConnected() {
			go b.wa.SyncGroups()
		}
	})
	b.wa.SetEventHandler(b.handleWhatsAppEvent)

	return b, nil
}

func (b *Bridge) Run(ctx context.Context) error {
	// Start WhatsApp connection
	if err := b.wa.Connect(); err != nil {
		b.log.Warn("initial whatsapp connect failed, will retry", "error", err)
	}

	// Drain any queued incoming messages on startup
	b.drainIncoming()

	// Start status ticker
	go b.statusLoop(ctx)

	// Start cleanup ticker
	go b.cleanupLoop(ctx)

	// Sweep expired media uploads periodically
	go func() {
		ticker := time.NewTicker(1 * time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				b.uploads.Sweep()
			}
		}
	}()

	// Register extra HTTP handlers
	b.srv.RegisterHandler("/pairing", b.handlePairingHTTP)
	b.srv.RegisterHandler("/upload", b.handleUploadHTTP)

	// Start server (blocks until ctx cancelled)
	return b.srv.Start(ctx)
}

func (b *Bridge) handlePairingHTTP(w http.ResponseWriter, r *http.Request) {
	b.pairMu.RLock()
	defer b.pairMu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"qr_code":      b.lastQR,
		"pairing_code": b.lastPair,
	})
}

// handleUploadHTTP accepts a multipart/form-encoded media upload and stores it
// in memory, returning an upload_id that can be referenced by a subsequent
// send_media WS message. This bypasses the WS frame size limit (~770KB usable
// after base64 expansion) so large files like PDFs can be sent.
func (b *Bridge) handleUploadHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	// Auth: reuse the same token as the WS endpoint.
	if r.URL.Query().Get("token") != b.cfg.Token {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	// Cap request body to 100 MiB (WhatsApp's own document limit).
	r.Body = http.MaxBytesReader(w, r.Body, 100<<20)
	if err := r.ParseMultipartForm(100 << 20); err != nil {
		b.log.Warn("upload parse failed", "error", err, "remote", r.RemoteAddr)
		http.Error(w, "upload parse failed: "+err.Error(), http.StatusBadRequest)
		return
	}

	file, header, err := r.FormFile("file")
	if err != nil {
		http.Error(w, "missing 'file' field", http.StatusBadRequest)
		return
	}
	defer file.Close()

	data := make([]byte, header.Size)
	if _, err := io.ReadFull(file, data); err != nil {
		b.log.Warn("upload read failed", "error", err)
		http.Error(w, "failed to read upload", http.StatusBadRequest)
		return
	}

	mime := r.FormValue("mime_type")
	if mime == "" {
		mime = header.Header.Get("Content-Type")
	}
	filename := r.FormValue("filename")
	if filename == "" {
		filename = header.Filename
	}

	id := b.uploads.Put(data, mime, filename)
	b.log.Info("upload received", "upload_id", id, "bytes", len(data), "mime_type", mime, "filename", filename)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"upload_id": id})
}

func (b *Bridge) handleWhatsAppEvent(event any) {
	switch evt := event.(type) {
	case whatsapp.ConnectedEvent:
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeConnected, wsproto.ConnectedPayload{}))
		b.drainOutgoing()
		go b.wa.SyncGroups()

	case whatsapp.DisconnectedEvent:
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeDisconnected, wsproto.DisconnectedPayload{
			Reason: evt.Reason,
		}))

	case whatsapp.QRCodeEvent:
		b.pairMu.Lock()
		b.lastQR = evt.Code
		b.pairMu.Unlock()
		b.log.Info("QR code generated")
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeQRCode, wsproto.QRCodePayload{
			QRString:  evt.Code,
			ExpiresAt: time.Now().Add(60 * time.Second).UTC().Format(time.RFC3339),
		}))

	case whatsapp.PairingCodeEvent:
		b.pairMu.Lock()
		b.lastPair = evt.Code
		b.pairMu.Unlock()
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypePairingCode, wsproto.PairingCodePayload{
			Code:        evt.Code,
			PhoneNumber: evt.PhoneNumber,
		}))

	case whatsapp.IncomingMessageEvent:
		payload := wsproto.IncomingMessagePayload{
			WhatsAppMessageID: evt.WhatsAppMessageID,
			ChatID:            evt.ChatID,
			ChatKind:          evt.ChatKind,
			SenderJID:         evt.SenderJID,
			SenderName:        evt.SenderName,
			Text:              evt.Text,
			QuotedMessageID:   evt.QuotedMessageID,
			MentionedJIDs:     evt.MentionedJIDs,
			Timestamp:         evt.Timestamp,
		}
		if evt.Media != nil {
			payload.Media = &wsproto.MediaInfo{
				MediaType: evt.Media.MediaType,
				MimeType:  evt.Media.MimeType,
				Filename:  evt.Media.Filename,
				SizeBytes: evt.Media.SizeBytes,
			}
		}
		if len(evt.Contacts) > 0 {
			payload.Contacts = make([]wsproto.SharedContact, len(evt.Contacts))
			for i, c := range evt.Contacts {
				payload.Contacts[i] = wsproto.SharedContact{
					DisplayName: c.DisplayName,
					Vcard:       c.Vcard,
					Phone:       c.Phone,
				}
			}
		}
		env := wsproto.NewEnvelope(wsproto.TypeIncomingMessage, payload)

		// Log incoming message with content
		textPreview := evt.Text
		if len(textPreview) > 100 {
			textPreview = textPreview[:100] + "..."
		}
		b.log.Info("incoming message",
			"msg_id", evt.WhatsAppMessageID,
			"chat", evt.ChatID,
			"kind", evt.ChatKind,
			"sender", evt.SenderName,
			"sender_jid", evt.SenderJID,
			"text", textPreview,
			"has_media", evt.Media != nil,
		)

		// Always enqueue for durability
		if err := b.inQ.Enqueue(queue.Incoming, evt.WhatsAppMessageID, wsproto.TypeIncomingMessage, payload); err != nil {
			b.log.Warn("failed to enqueue incoming message", "error", err, "msg_id", evt.WhatsAppMessageID)
		}

		// Try to send immediately if client connected
		if !b.sendToClient(env) {
			b.log.Info("client not connected, incoming message queued", "msg_id", evt.WhatsAppMessageID)
		}

	case whatsapp.MessageAckedEvent:
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeMessageAcked, wsproto.MessageAckedPayload{
			WhatsAppMessageID: evt.WhatsAppMessageID,
			ChatID:            evt.ChatID,
			AckType:           evt.AckType,
		}))

	case whatsapp.SendMessageResultEvent:
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeSendMessageResult, wsproto.SendMessageResultPayload{
			RequestID:         evt.RequestID,
			Success:           evt.Success,
			WhatsAppMessageID: evt.WhatsAppMessageID,
			Error:             evt.Error,
		}))
		if evt.Success {
			b.outQ.MarkDelivered(evt.RequestID)
		} else {
			b.outQ.MarkFailed(evt.RequestID)
		}

	case whatsapp.GroupMemberChangeEvent:
		payload := wsproto.GroupMemberChangePayload{
			GroupJID:   evt.GroupJID,
			GroupName:  evt.GroupName,
			SenderJID:  evt.SenderJID,
			JoinedJIDs: evt.JoinedJIDs,
			LeftJIDs:   evt.LeftJIDs,
			Timestamp:  evt.Timestamp,
		}
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeGroupMemberChange, payload))

	case whatsapp.GroupSyncEvent:
		payload := wsproto.GroupSyncPayload{
			GroupJID:    evt.GroupJID,
			GroupName:   evt.GroupName,
			Description: evt.Description,
			Timestamp:   evt.Timestamp,
		}
		for _, p := range evt.Participants {
			payload.Participants = append(payload.Participants, wsproto.GroupParticipantPayload{
				JID:          p.JID,
				DisplayName:  p.DisplayName,
				IsAdmin:      p.IsAdmin,
				IsSuperAdmin: p.IsSuperAdmin,
			})
		}
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeGroupSync, payload))

	case whatsapp.ChatPresenceEvent:
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeChatPresence, wsproto.ChatPresencePayload{
			ChatJID:   evt.ChatJID,
			SenderJID: evt.SenderJID,
			Media:     evt.Media,
			Timestamp: evt.Timestamp,
		}))
	}
}

func (b *Bridge) handleClientMessage(env wsproto.Envelope) {
	switch env.Type {
	case wsproto.TypeSendMessage:
		var payload wsproto.SendMessagePayload
		if err := json.Unmarshal(env.Payload, &payload); err != nil {
			b.log.Warn("invalid send_message payload", "error", err)
			return
		}
		b.handleSend(payload)

	case wsproto.TypeSendMedia:
		var payload wsproto.SendMediaPayload
		if err := json.Unmarshal(env.Payload, &payload); err != nil {
			b.log.Warn("invalid send_media payload", "error", err)
			return
		}
		b.handleSendMedia(payload)

	case wsproto.TypeAck:
		var payload wsproto.AckPayload
		if err := json.Unmarshal(env.Payload, &payload); err != nil {
			b.log.Warn("invalid ack payload", "error", err)
			return
		}
		b.inQ.MarkDelivered(payload.MessageID)

	case wsproto.TypeRequestPairing:
		var payload wsproto.RequestPairingPayload
		if err := json.Unmarshal(env.Payload, &payload); err != nil {
			b.log.Warn("invalid request_pairing payload", "error", err)
			return
		}
		b.handlePairingRequest(payload)

	case wsproto.TypeSubscribePresence:
		var payload wsproto.SubscribePresencePayload
		if err := json.Unmarshal(env.Payload, &payload); err != nil {
			b.log.Warn("invalid subscribe_presence payload", "error", err)
			return
		}
		if err := b.wa.SubscribePresence(payload.ChatJID); err != nil {
			b.log.Warn("failed to subscribe presence", "chat_id", payload.ChatJID, "error", err)
		}
	}
}

func (b *Bridge) handleSend(payload wsproto.SendMessagePayload) {
	// Always enqueue for durability
	if err := b.outQ.Enqueue(queue.Outgoing, payload.RequestID, wsproto.TypeSendMessage, payload); err != nil {
		b.log.Warn("failed to enqueue outgoing message", "error", err, "request_id", payload.RequestID)
	}

	// Send immediately if WhatsApp is connected
	if !b.wa.IsConnected() {
		b.log.Info("whatsapp not connected, outgoing message queued", "request_id", payload.RequestID)
		return
	}

	textPreview := payload.Text
	if len(textPreview) > 100 {
		textPreview = textPreview[:100] + "..."
	}
	b.log.Info("outgoing message",
		"request_id", payload.RequestID,
		"chat_id", payload.ChatID,
		"text", textPreview,
	)

	jid, ok := b.wa.ParseJID(payload.ChatID)
	if !ok {
		b.log.Warn("invalid jid", "chat_id", payload.ChatID)
		b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
			RequestID: payload.RequestID,
			Success:   false,
			Error:     "invalid jid",
		})
		return
	}

	msgID, err := b.wa.SendMessage(jid, payload.Text)
	if err != nil {
		b.log.Warn("send failed", "error", err, "chat_id", payload.ChatID)
		b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
			RequestID: payload.RequestID,
			Success:   false,
			Error:     err.Error(),
		})
		return
	}

	b.log.Info("message sent", "request_id", payload.RequestID, "msg_id", msgID, "chat_id", payload.ChatID)
	b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
		RequestID:         payload.RequestID,
		Success:           true,
		WhatsAppMessageID: msgID,
	})
}

func (b *Bridge) handlePairingRequest(payload wsproto.RequestPairingPayload) {
	if payload.Method == "phone_code" && payload.PhoneNumber != "" {
		code, err := b.wa.RequestPairingCode(payload.PhoneNumber)
		if err != nil {
			b.log.Warn("pairing code request failed", "error", err)
			return
		}
		b.handleWhatsAppEvent(whatsapp.PairingCodeEvent{
			Code:        code,
			PhoneNumber: payload.PhoneNumber,
		})
	} else {
		// QR code is handled automatically by whatsmeow events
		b.wa.RequestQRCode()
	}
}

func (b *Bridge) handleSendMedia(payload wsproto.SendMediaPayload) {
	if !b.wa.IsConnected() {
		b.log.Warn("whatsapp not connected, cannot send media", "request_id", payload.RequestID)
		b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
			RequestID: payload.RequestID,
			Success:   false,
			Error:     "whatsapp not connected",
		})
		return
	}

	jid, ok := b.wa.ParseJID(payload.ChatID)
	if !ok {
		b.log.Warn("invalid jid for media send", "chat_id", payload.ChatID)
		b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
			RequestID: payload.RequestID,
			Success:   false,
			Error:     "invalid jid",
		})
		return
	}

	// Resolve media bytes: prefer upload_id (preferred path), fall back to legacy inline base64.
	var (
		data     []byte
		fileName string
		err      error
	)
	if payload.UploadID != "" {
		entry, ok := b.uploads.Take(payload.UploadID)
		if !ok {
			b.log.Warn("upload not found or expired", "upload_id", payload.UploadID, "request_id", payload.RequestID)
			b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
				RequestID: payload.RequestID,
				Success:   false,
				Error:     "upload not found or expired",
			})
			return
		}
		data = entry.Data
		fileName = entry.FileName
		b.log.Info("resolved upload", "upload_id", payload.UploadID, "bytes", len(data), "filename", fileName, "request_id", payload.RequestID)
	} else {
		data, err = base64.StdEncoding.DecodeString(payload.Data)
		if err != nil {
			b.log.Warn("invalid base64 data for media send", "error", err, "request_id", payload.RequestID)
			b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
				RequestID: payload.RequestID,
				Success:   false,
				Error:     "invalid base64 data",
			})
			return
		}
	}

	var msgID string
	mime := strings.ToLower(payload.MimeType)
	if mime == "image/gif" {
		msgID, err = b.wa.SendGIF(jid, data, payload.Caption)
	} else if strings.HasPrefix(mime, "video/") {
		// WhatsApp stores/transmits GIFs as MP4; send all video/* as animated
		// GIFs so they render inline in chat instead of as file attachments.
		msgID, err = b.wa.SendVideoAsGif(jid, data, payload.Caption)
	} else if strings.HasPrefix(mime, "image/") {
		msgID, err = b.wa.SendImage(jid, data, payload.MimeType, payload.Caption)
	} else {
		name := fileName
		if name == "" {
			name = "file"
		}
		msgID, err = b.wa.SendDocument(jid, data, payload.MimeType, name, payload.Caption)
	}

	if err != nil {
		b.log.Warn("media send failed", "error", err, "chat_id", payload.ChatID, "request_id", payload.RequestID)
		b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
			RequestID: payload.RequestID,
			Success:   false,
			Error:     err.Error(),
		})
		return
	}

	b.log.Info("media sent", "chat_id", payload.ChatID, "mime_type", payload.MimeType, "request_id", payload.RequestID, "msg_id", msgID, "caption", payload.Caption)
	b.handleWhatsAppEvent(whatsapp.SendMessageResultEvent{
		RequestID:         payload.RequestID,
		Success:           true,
		WhatsAppMessageID: msgID,
	})
}

func (b *Bridge) drainIncoming() {
	msgs, err := b.inQ.DrainPending(queue.Incoming)
	if err != nil {
		b.log.Warn("failed to drain incoming queue", "error", err)
		return
	}
	if len(msgs) > 0 {
		b.log.Info("draining incoming queue", "count", len(msgs))
	}
	for _, m := range msgs {
		env, err := m.Envelope()
		if err != nil {
			b.log.Warn("failed to build envelope from queued message", "error", err)
			continue
		}
		if b.sendToClient(env) {
			b.log.Info("drained queued incoming message", "msg_id", m.MessageID)
		} else {
			// No client connected, reset to pending for next attempt
			b.inQ.MarkPending(m.MessageID)
			return // No point continuing
		}
	}
}

func (b *Bridge) drainOutgoing() {
	msgs, err := b.outQ.DrainPending(queue.Outgoing)
	if err != nil {
		b.log.Warn("failed to drain outgoing queue", "error", err)
		return
	}
	if len(msgs) > 0 {
		b.log.Info("draining outgoing queue", "count", len(msgs))
	}
	for _, m := range msgs {
		var payload wsproto.SendMessagePayload
		if err := json.Unmarshal([]byte(m.PayloadJSON), &payload); err != nil {
			b.log.Warn("failed to parse queued outgoing message", "error", err)
			b.outQ.MarkFailed(m.MessageID)
			continue
		}

		jid, ok := b.wa.ParseJID(payload.ChatID)
		if !ok {
			b.outQ.MarkFailed(m.MessageID)
			continue
		}

		msgID, err := b.wa.SendMessage(jid, payload.Text)
		if err != nil {
			b.log.Warn("failed to send queued message", "error", err, "request_id", payload.RequestID)
			if m.Attempts >= b.cfg.OutgoingMaxRetries {
				b.outQ.MarkFailed(m.MessageID)
				b.sendToClient(wsproto.NewEnvelope(wsproto.TypeSendMessageResult, wsproto.SendMessageResultPayload{
					RequestID: payload.RequestID,
					Success:   false,
					Error:     "max retries exceeded",
				}))
			} else {
				b.outQ.MarkPending(m.MessageID)
			}
			continue
		}

		b.outQ.MarkDelivered(m.MessageID)
		b.sendToClient(wsproto.NewEnvelope(wsproto.TypeSendMessageResult, wsproto.SendMessageResultPayload{
			RequestID:         payload.RequestID,
			Success:           true,
			WhatsAppMessageID: msgID,
		}))
		b.log.Info("drained queued outgoing message", "msg_id", msgID)
	}
}

func (b *Bridge) sendToClient(env wsproto.Envelope) bool {
	return b.srv.Send(env)
}

func (b *Bridge) statusLoop(ctx context.Context) {
	ticker := time.NewTicker(b.cfg.StatusInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			inCount, _ := b.inQ.PendingCount(queue.Incoming)
			outCount, _ := b.outQ.PendingCount(queue.Outgoing)
			b.sendToClient(wsproto.NewEnvelope(wsproto.TypeBridgeStatus, wsproto.BridgeStatusPayload{
				WhatsAppConnected: b.wa.IsConnected(),
				ServerClients:     b.srv.ClientCount(),
				IncomingQueueSize: inCount,
				OutgoingQueueSize: outCount,
				UptimeSeconds:     b.srv.Uptime(),
			}))
		}
	}
}

func (b *Bridge) cleanupLoop(ctx context.Context) {
	ticker := time.NewTicker(1 * time.Hour)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			b.inQ.Cleanup()
			b.outQ.Cleanup()
		}
	}
}
