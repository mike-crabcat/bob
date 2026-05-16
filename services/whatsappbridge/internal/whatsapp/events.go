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

type IncomingMessageEvent struct {
	WhatsAppMessageID string
	ChatID            string
	ChatKind          string
	SenderJID         string
	SenderName        string
	Text              string
	QuotedMessageID   string
	Contacts          []SharedContact
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
