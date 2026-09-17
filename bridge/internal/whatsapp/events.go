package whatsapp

type ConnectedEvent struct{}

type DisconnectedEvent struct {
	Reason string
}

type QRCodeEvent struct {
	Code string
}

type PairingCodeEvent struct {
	Code        string
	PhoneNumber string
}

type SharedContact struct {
	DisplayName string
	Vcard       string
	Phone       string
}

type MediaInfo struct {
	MediaType string // "image", "document", "audio", "video"
	MimeType  string
	Filename  string
	SizeBytes int64
	FilePath  string // absolute path on disk
}

type IncomingMessageEvent struct {
	WhatsAppMessageID string
	ChatID            string
	ChatKind          string
	SenderJID         string
	SenderName        string
	Text              string
	QuotedMessageID   string
	QuotedSenderJID   string
	QuotedText        string
	MentionedJIDs     []string
	Contacts          []SharedContact
	Media             *MediaInfo
	Timestamp         string
}

// IncomingReactionEvent is an emoji reaction on an existing message.
// Emoji == "" means the sender removed their reaction. TargetMessageID /
// TargetSenderJID identify the reacted-to message (its author's JID, which
// groups need to disambiguate the target).
type IncomingReactionEvent struct {
	WhatsAppMessageID string
	ChatID            string
	ChatKind          string
	SenderJID         string
	SenderName        string
	TargetMessageID   string
	TargetSenderJID   string
	Emoji             string
	Timestamp         string
}

type MessageAckedEvent struct {
	WhatsAppMessageID string
	ChatID            string
	AckType           string
}

type SendMessageResultEvent struct {
	RequestID         string
	Success           bool
	WhatsAppMessageID string
	Error             string
}

type GroupMemberChangeEvent struct {
	GroupJID   string
	GroupName  string
	SenderJID  string
	JoinedJIDs []string
	LeftJIDs   []string
	Timestamp  string
}

type GroupSyncEvent struct {
	GroupJID     string
	GroupName    string
	Description  string
	Participants []GroupParticipantInfo
	Timestamp    string
}

type GroupParticipantInfo struct {
	JID          string
	DisplayName  string
	IsAdmin      bool
	IsSuperAdmin bool
}

type ChatPresenceEvent struct {
	ChatJID   string
	SenderJID string
	Media     string // "text" or "audio"
	Timestamp string
}
