package wsproto

import (
	"encoding/json"
	"time"

	"github.com/google/uuid"
)

// Envelope is the top-level JSON structure for all messages.
type Envelope struct {
	Type      string          `json:"type"`
	ID        string          `json:"id"`
	Timestamp string          `json:"timestamp"`
	Payload   json.RawMessage `json:"payload,omitempty"`
}

func NewEnvelope(msgType string, payload any) Envelope {
	raw, _ := json.Marshal(payload)
	return Envelope{
		Type:      msgType,
		ID:        uuid.New().String(),
		Timestamp: time.Now().UTC().Format(time.RFC3339Nano),
		Payload:   raw,
	}
}

func (e Envelope) Marshal() ([]byte, error) {
	return json.Marshal(e)
}

func ParseEnvelope(data []byte) (Envelope, error) {
	var e Envelope
	err := json.Unmarshal(data, &e)
	return e, err
}

// --- Upstream messages (bridge → bob) ---

type ConnectedPayload struct {
	PhoneNumber string `json:"phone_number"`
	DeviceName  string `json:"device_name,omitempty"`
}

type DisconnectedPayload struct {
	Reason string `json:"reason"` // "connection_lost", "logged_out", "device_removed"
}

type QRCodePayload struct {
	QRString  string `json:"qr_string"`
	ExpiresAt string `json:"expires_at"`
}

type PairingCodePayload struct {
	Code        string `json:"code"`
	PhoneNumber string `json:"phone_number"`
}

type IncomingMessagePayload struct {
	WhatsAppMessageID string           `json:"whatsapp_message_id"`
	ChatID            string           `json:"chat_id"`
	ChatKind          string           `json:"chat_kind"` // "dm" or "group"
	SenderJID         string           `json:"sender_jid"`
	SenderName        string           `json:"sender_name,omitempty"`
	Text              string           `json:"text,omitempty"`
	QuotedMessageID   string           `json:"quoted_message_id,omitempty"`
	MentionedJIDs     []string         `json:"mentioned_jids,omitempty"`
	Media             *MediaInfo       `json:"media,omitempty"`
	Contacts          []SharedContact  `json:"contacts,omitempty"`
	Timestamp         string           `json:"timestamp"`
}

type MediaInfo struct {
	MediaType string `json:"media_type"` // "image", "document", "audio", "video"
	MimeType  string `json:"mime_type"`
	Filename  string `json:"filename,omitempty"`
	SizeBytes int64  `json:"size_bytes"`
	Data      string `json:"data,omitempty"` // base64
}

type SharedContact struct {
	DisplayName string `json:"display_name"`
	Vcard       string `json:"vcard,omitempty"`
	Phone       string `json:"phone,omitempty"` // first TEL from vcard, normalized
}

type MessageAckedPayload struct {
	WhatsAppMessageID string `json:"whatsapp_message_id"`
	ChatID            string `json:"chat_id"`
	AckType           string `json:"ack_type"` // "delivered", "read"
}

type BridgeStatusPayload struct {
	WhatsAppConnected bool  `json:"whatsapp_connected"`
	ServerClients     int   `json:"server_clients"`
	IncomingQueueSize int   `json:"incoming_queue_size"`
	OutgoingQueueSize int   `json:"outgoing_queue_size"`
	UptimeSeconds     int64 `json:"uptime_seconds"`
}

type SendMessageResultPayload struct {
	RequestID         string `json:"request_id"`
	Success           bool   `json:"success"`
	WhatsAppMessageID string `json:"whatsapp_message_id,omitempty"`
	Error             string `json:"error,omitempty"`
}

type GroupMemberChangePayload struct {
	GroupJID   string   `json:"group_jid"`
	GroupName  string   `json:"group_name,omitempty"`
	SenderJID  string   `json:"sender_jid,omitempty"`
	JoinedJIDs []string `json:"joined_jids,omitempty"`
	LeftJIDs   []string `json:"left_jids,omitempty"`
	Timestamp  string   `json:"timestamp"`
}

type GroupSyncPayload struct {
	GroupJID     string                    `json:"group_jid"`
	GroupName    string                    `json:"group_name,omitempty"`
	Description  string                    `json:"description,omitempty"`
	Participants []GroupParticipantPayload `json:"participants"`
	Timestamp    string                    `json:"timestamp"`
}

type GroupParticipantPayload struct {
	JID          string `json:"jid"`
	DisplayName  string `json:"display_name,omitempty"`
	IsAdmin      bool   `json:"is_admin"`
	IsSuperAdmin bool   `json:"is_super_admin"`
}

// --- Downstream messages (bob → bridge) ---

type SendMessagePayload struct {
	ChatID             string `json:"chat_id"`
	Text               string `json:"text"`
	ReplyToMessageID   string `json:"reply_to_message_id,omitempty"`
	RequestID          string `json:"request_id"`
}

type SendMediaPayload struct {
	ChatID    string `json:"chat_id"`
	MimeType  string `json:"mime_type"`
	Data      string `json:"data"`      // base64-encoded
	Caption   string `json:"caption,omitempty"`
	RequestID string `json:"request_id"`
}

type AckPayload struct {
	MessageID string `json:"message_id"`
}

type ChatPresencePayload struct {
	ChatJID   string `json:"chat_id"`
	SenderJID string `json:"sender_jid"`
	Media     string `json:"media"`
	Timestamp string `json:"timestamp"`
}

type SubscribePresencePayload struct {
	ChatJID string `json:"chat_id"`
}

type RequestPairingPayload struct {
	Method      string `json:"method"`       // "qr" or "phone_code"
	PhoneNumber string `json:"phone_number,omitempty"`
}

// --- Message type constants ---

const (
	TypeConnected        = "whatsapp.connected"
	TypeDisconnected     = "whatsapp.disconnected"
	TypeQRCode           = "whatsapp.qr_code"
	TypePairingCode      = "whatsapp.pairing_code"
	TypeIncomingMessage  = "whatsapp.incoming_message"
	TypeMessageAcked     = "whatsapp.message_acked"
	TypeBridgeStatus     = "bridge.status"
	TypeSendMessage      = "send_message"
	TypeSendMedia        = "send_media"
	TypeAck              = "ack"
	TypeRequestPairing   = "request_pairing"
	TypeSendMessageResult  = "send_message_result"
	TypeGroupMemberChange  = "whatsapp.group_member_change"
	TypeGroupSync          = "whatsapp.group_sync"
	TypeChatPresence       = "whatsapp.chat_presence"
	TypeSubscribePresence  = "subscribe_presence"
)
