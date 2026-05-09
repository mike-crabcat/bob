package whatsapp

import (
	"context"
	"fmt"
	"log/slog"
	"sync"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

type EventHandler func(event any)

type Client struct {
	log       *slog.Logger
	client    *whatsmeow.Client
	container *sqlstore.Container

	mu        sync.RWMutex
	connected bool

	onEvent EventHandler
}

func NewClient(sessionDBPath string, log *slog.Logger) (*Client, error) {
	container, err := sqlstore.New(
		context.Background(),
		"sqlite3",
		fmt.Sprintf("file:%s?_journal_mode=WAL&_foreign_keys=on", sessionDBPath),
		nil,
	)
	if err != nil {
		return nil, fmt.Errorf("create whatsmeow container: %w", err)
	}

	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		return nil, fmt.Errorf("get device store: %w", err)
	}

	client := whatsmeow.NewClient(deviceStore, nil)
	client.EnableAutoReconnect = true
	client.AutoTrustIdentity = true

	c := &Client{
		log:       log,
		client:    client,
		container: container,
	}

	client.AddEventHandler(c.handleEvent)
	return c, nil
}

func (c *Client) SetEventHandler(handler EventHandler) {
	c.onEvent = handler
}

func (c *Client) Connect() error {
	return c.client.Connect()
}

func (c *Client) Disconnect() {
	c.client.Disconnect()
	c.mu.Lock()
	c.connected = false
	c.mu.Unlock()
}

func (c *Client) IsConnected() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.connected
}

func (c *Client) IsLoggedIn() bool {
	return c.client.IsLoggedIn()
}

func (c *Client) RequestQRCode() error {
	if c.client.IsConnected() {
		return nil
	}
	return c.client.Connect()
}

func (c *Client) RequestPairingCode(phone string) (string, error) {
	code, err := c.client.PairPhone(context.Background(), phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
	if err != nil {
		return "", fmt.Errorf("pair phone: %w", err)
	}
	return code, nil
}

func (c *Client) SendMessage(jid types.JID, text string) (string, error) {
	msg := &waE2E.Message{
		Conversation: &text,
	}
	resp, err := c.client.SendMessage(context.Background(), jid, msg)
	if err != nil {
		return "", err
	}
	return string(resp.ServerID), nil
}

func (c *Client) ParseJID(s string) (types.JID, bool) {
	jid, err := types.ParseJID(s)
	if err != nil {
		return jid, false
	}
	return jid, jid.Server == types.DefaultUserServer || jid.Server == types.HiddenUserServer || jid.Server == types.GroupServer
}

// ResolveLID resolves a LID (linked ID) to a phone number JID.
func (c *Client) ResolveLID(jid types.JID) types.JID {
	if jid.Server != types.HiddenUserServer {
		return jid
	}
	if c.client.Store.LIDs == nil {
		return jid
	}
	pn, err := c.client.Store.LIDs.GetPNForLID(context.Background(), jid)
	if err != nil || pn.IsEmpty() {
		c.log.Debug("could not resolve LID to PN", "lid", jid.String(), "error", err)
		return jid
	}
	return pn
}

func (c *Client) handleEvent(raw any) {
	switch evt := raw.(type) {
	case *events.Connected:
		c.mu.Lock()
		c.connected = true
		c.mu.Unlock()
		c.log.Info("whatsapp connected")
		if c.onEvent != nil {
			c.onEvent(ConnectedEvent{})
		}

	case *events.Disconnected:
		c.mu.Lock()
		c.connected = false
		c.mu.Unlock()
		c.log.Warn("whatsapp disconnected")
		if c.onEvent != nil {
			c.onEvent(DisconnectedEvent{Reason: "connection_lost"})
		}

	case *events.LoggedOut:
		c.mu.Lock()
		c.connected = false
		c.mu.Unlock()
		c.log.Warn("whatsapp logged out")
		if c.onEvent != nil {
			c.onEvent(DisconnectedEvent{Reason: "logged_out"})
		}

	case *events.Message:
		c.handleMessage(evt)

	case *events.Receipt:
		c.handleReceipt(evt)

	case *events.QR:
		if c.onEvent != nil && len(evt.Codes) > 0 {
			c.onEvent(QRCodeEvent{Code: evt.Codes[0]})
		}

	case *events.PairSuccess:
		c.log.Info("pair success", "jid", evt.ID.String())
	}
}

func (c *Client) handleMessage(evt *events.Message) {
	info := evt.Info
	text := ""
	if evt.Message.GetConversation() != "" {
		text = evt.Message.GetConversation()
	} else if ext := evt.Message.GetExtendedTextMessage(); ext != nil {
		text = ext.GetText()
	}

	if text == "" {
		return
	}

	chatKind := "dm"
	if info.IsGroup {
		chatKind = "group"
	}

	senderName := ""
	if info.PushName != "" {
		senderName = info.PushName
	}

	// Resolve LIDs to phone number JIDs
	chatJID := c.ResolveLID(info.Chat)
	senderJID := c.ResolveLID(info.Sender)

	msgEvt := IncomingMessageEvent{
		WhatsAppMessageID: info.ID,
		ChatID:            chatJID.String(),
		ChatKind:          chatKind,
		SenderJID:         senderJID.String(),
		SenderName:        senderName,
		Text:              text,
		Timestamp:         info.Timestamp.UTC().Format("2006-01-02T15:04:05.000Z"),
	}

	if ext := evt.Message.GetExtendedTextMessage(); ext != nil {
		msgEvt.QuotedMessageID = ext.GetContextInfo().GetStanzaID()
	}

	if c.onEvent != nil {
		c.onEvent(msgEvt)
	}
}

func (c *Client) handleReceipt(evt *events.Receipt) {
	if evt.Type == types.ReceiptTypeRead || evt.Type == types.ReceiptTypeReadSelf {
		if c.onEvent != nil {
			for _, msgID := range evt.MessageIDs {
				c.onEvent(MessageAckedEvent{
					WhatsAppMessageID: msgID,
					ChatID:            evt.Chat.String(),
					AckType:           "read",
				})
			}
		}
	}
}

func (c *Client) Close() error {
	c.client.Disconnect()
	return c.container.Close()
}
