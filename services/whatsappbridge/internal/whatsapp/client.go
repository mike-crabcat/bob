package whatsapp

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

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
	mediaDir  string

	mu        sync.RWMutex
	connected bool

	onEvent EventHandler
}

func NewClient(sessionDBPath string, mediaDir string, log *slog.Logger) (*Client, error) {
	// Resolve the actual media directory: explicit env var wins, otherwise use
	// <dataDir>/media for backward compatibility.
	resolvedMediaDir := os.Getenv("WHATSAPPBRIDGE_MEDIA_DIR")
	if resolvedMediaDir == "" {
		resolvedMediaDir = filepath.Join(mediaDir, "media")
	}

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
		mediaDir:  resolvedMediaDir,
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

func (c *Client) SendImage(jid types.JID, imageData []byte, mimeType, caption string) (string, error) {
	uploaded, err := c.client.Upload(context.Background(), imageData, whatsmeow.MediaImage)
	if err != nil {
		return "", fmt.Errorf("upload image: %w", err)
	}

	msg := &waE2E.Message{
		ImageMessage: &waE2E.ImageMessage{
			Mimetype:      &mimeType,
			MediaKey:      uploaded.MediaKey,
			FileEncSHA256: uploaded.FileEncSHA256,
			FileSHA256:    uploaded.FileSHA256,
			FileLength:    &uploaded.FileLength,
			URL:           &uploaded.URL,
			DirectPath:    &uploaded.DirectPath,
		},
	}
	if caption != "" {
		msg.ImageMessage.Caption = &caption
	}

	resp, err := c.client.SendMessage(context.Background(), jid, msg)
	if err != nil {
		return "", err
	}
	return string(resp.ServerID), nil
}

func (c *Client) SendDocument(jid types.JID, data []byte, mimeType, fileName, caption string) (string, error) {
	uploaded, err := c.client.Upload(context.Background(), data, whatsmeow.MediaDocument)
	if err != nil {
		return "", fmt.Errorf("upload document: %w", err)
	}

	msg := &waE2E.Message{
		DocumentMessage: &waE2E.DocumentMessage{
			Mimetype:      &mimeType,
			FileName:      &fileName,
			MediaKey:      uploaded.MediaKey,
			FileEncSHA256: uploaded.FileEncSHA256,
			FileSHA256:    uploaded.FileSHA256,
			FileLength:    &uploaded.FileLength,
			URL:           &uploaded.URL,
			DirectPath:    &uploaded.DirectPath,
		},
	}
	if caption != "" {
		msg.DocumentMessage.Caption = &caption
	}

	resp, err := c.client.SendMessage(context.Background(), jid, msg)
	if err != nil {
		return "", err
	}
	return string(resp.ServerID), nil
}

// SendGIF converts GIF data to a silent MP4 via ffmpeg, then uploads and sends
// it as a VideoMessage with GifPlayback so WhatsApp displays it as an animated GIF.
func (c *Client) SendGIF(jid types.JID, gifData []byte, caption string) (string, error) {
	mp4Data, err := gifToMP4(gifData)
	if err != nil {
		c.log.Warn("gif to mp4 conversion failed, sending as document", "error", err)
		return c.SendDocument(jid, gifData, "image/gif", "animation.gif", caption)
	}
	return c.SendVideoAsGif(jid, mp4Data, caption)
}

// SendVideoAsGif uploads MP4 data as a VideoMessage with GifPlayback=true so
// WhatsApp renders it inline as an animated GIF. Used for MP4 files that are
// already in the right format (e.g. GIFs received from WhatsApp and re-sent).
func (c *Client) SendVideoAsGif(jid types.JID, mp4Data []byte, caption string) (string, error) {
	uploaded, err := c.client.Upload(context.Background(), mp4Data, whatsmeow.MediaVideo)
	if err != nil {
		return "", fmt.Errorf("upload gif video: %w", err)
	}

	mimeType := "video/mp4"
	gifPlayback := true
	msg := &waE2E.Message{
		VideoMessage: &waE2E.VideoMessage{
			Mimetype:      &mimeType,
			GifPlayback:   &gifPlayback,
			MediaKey:      uploaded.MediaKey,
			FileEncSHA256: uploaded.FileEncSHA256,
			FileSHA256:    uploaded.FileSHA256,
			FileLength:    &uploaded.FileLength,
			URL:           &uploaded.URL,
			DirectPath:    &uploaded.DirectPath,
		},
	}
	if caption != "" {
		msg.VideoMessage.Caption = &caption
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
// For s.whatsapp.net JIDs, it also normalizes phone numbers that have
// extra trailing digits by checking progressively shorter prefixes against
// the LID store.
func (c *Client) ResolveLID(jid types.JID) types.JID {
	if jid.Server == types.HiddenUserServer {
		return c.resolveLIDToPN(jid)
	}
	if jid.Server == types.DefaultUserServer {
		return c.normalizePhoneJID(jid)
	}
	return jid
}

func (c *Client) resolveLIDToPN(lid types.JID) types.JID {
	if c.client.Store.LIDs == nil {
		return lid
	}
	pn, err := c.client.Store.LIDs.GetPNForLID(context.Background(), lid)
	if err != nil || pn.IsEmpty() {
		c.log.Debug("could not resolve LID to PN", "lid", lid.String(), "error", err)
		return lid
	}
	return pn
}

// normalizePhoneJID checks if a phone number JID has extra trailing digits
// by looking it up in the LID store. If the full number isn't known, it tries
// progressively shorter prefixes to find the correct phone number.
func (c *Client) normalizePhoneJID(jid types.JID) types.JID {
	if c.client.Store.LIDs == nil || len(jid.User) < 8 {
		return jid
	}

	// First check if the full number is already known
	lid, err := c.client.Store.LIDs.GetLIDForPN(context.Background(), jid)
	if err == nil && !lid.IsEmpty() {
		return jid
	}

	// Try shorter prefixes to find the real phone number
	for n := len(jid.User) - 1; n >= 8; n-- {
		candidate := types.JID{User: jid.User[:n], Server: types.DefaultUserServer}
		lid, err := c.client.Store.LIDs.GetLIDForPN(context.Background(), candidate)
		if err == nil && !lid.IsEmpty() {
			c.log.Info("normalized phone number JID by trimming extra digits",
				"from", jid.User, "to", candidate.User)
			return candidate
		}
	}

	return jid
}

func (c *Client) handleEvent(raw any) {
	switch evt := raw.(type) {
	case *events.Connected:
		c.mu.Lock()
		c.connected = true
		c.mu.Unlock()
		c.log.Info("whatsapp connected")
		// Mark as available so we receive typing/presence notifications
		if err := c.client.SendPresence(context.Background(), types.PresenceAvailable); err != nil {
			c.log.Warn("failed to send available presence", "error", err)
		}
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

	case *events.JoinedGroup:
		c.handleJoinedGroup(evt)

	case *events.GroupInfo:
		c.handleGroupInfo(evt)

	case *events.ChatPresence:
		if evt.State == types.ChatPresenceComposing {
			if c.onEvent != nil {
				senderJID := c.ResolveLID(evt.Sender)
				c.onEvent(ChatPresenceEvent{
					ChatJID:   evt.Chat.String(),
					SenderJID: senderJID.String(),
					Media:     string(evt.Media),
					Timestamp: time.Now().UTC().Format("2006-01-02T15:04:05.000Z"),
				})
			}
		}
	}
}

func (c *Client) handleMessage(evt *events.Message) {
	info := evt.Info
	text, contacts := extractTextAndContacts(evt.Message)

	img := evt.Message.GetImageMessage()
	vid := evt.Message.GetVideoMessage()
	if text == "" && img == nil && vid == nil {
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
		Contacts:          contacts,
		Timestamp:         info.Timestamp.UTC().Format("2006-01-02T15:04:05.000Z"),
	}

	// Download and save image if present
	if img != nil {
		data, err := c.client.Download(context.Background(), img)
		if err != nil {
			c.log.Warn("failed to download image", "error", err, "msg_id", info.ID)
		} else {
			os.MkdirAll(c.mediaDir, 0755)
			filename := info.ID + ".jpg"
			fullPath := filepath.Join(c.mediaDir, filename)
			if err := os.WriteFile(fullPath, data, 0644); err != nil {
				c.log.Warn("failed to save image", "error", err)
			} else {
				mimeType := img.GetMimetype()
				if mimeType == "" {
					mimeType = "image/jpeg"
				}
				msgEvt.Media = &MediaInfo{
					MediaType: "image",
					MimeType:  mimeType,
					Filename:  filename,
					SizeBytes: int64(len(data)),
					FilePath:  fullPath,
				}
				c.log.Info("saved incoming image", "msg_id", info.ID, "size", len(data), "path", fullPath)
			}
		}
	}

	// Download and save video or GIF if present. WhatsApp encodes animated GIFs
	// as VideoMessage with GifPlayback=true; bytes are always MP4-encoded.
	if vid != nil {
		data, err := c.client.Download(context.Background(), vid)
		if err != nil {
			c.log.Warn("failed to download video", "error", err, "msg_id", info.ID)
		} else {
			os.MkdirAll(c.mediaDir, 0755)
			filename := info.ID + ".mp4"
			fullPath := filepath.Join(c.mediaDir, filename)
			if err := os.WriteFile(fullPath, data, 0644); err != nil {
				c.log.Warn("failed to save video", "error", err)
			} else {
				mimeType := vid.GetMimetype()
				if mimeType == "" {
					mimeType = "video/mp4"
				}
				mediaType := "video"
				if vid.GetGifPlayback() {
					mediaType = "gif"
				}
				msgEvt.Media = &MediaInfo{
					MediaType: mediaType,
					MimeType:  mimeType,
					Filename:  filename,
					SizeBytes: int64(len(data)),
					FilePath:  fullPath,
				}
				c.log.Info("saved incoming video", "msg_id", info.ID, "kind", mediaType, "size", len(data), "path", fullPath)
			}
		}
	}

	if ext := evt.Message.GetExtendedTextMessage(); ext != nil {
		msgEvt.QuotedMessageID = ext.GetContextInfo().GetStanzaID()
		for _, raw := range ext.GetContextInfo().GetMentionedJID() {
			parsed, err := types.ParseJID(raw)
			if err != nil {
				msgEvt.MentionedJIDs = append(msgEvt.MentionedJIDs, raw)
				continue
			}
			resolved := c.ResolveLID(parsed)
			msgEvt.MentionedJIDs = append(msgEvt.MentionedJIDs, resolved.String())
			// Replace LID digits in text with resolved phone number digits
			if resolved.Server != types.HiddenUserServer && resolved.User != parsed.User {
				msgEvt.Text = strings.Replace(msgEvt.Text, "@"+parsed.User, "@"+resolved.User, 1)
			}
		}
	}

	if c.onEvent != nil {
		c.onEvent(msgEvt)
	}
}

// extractTextAndContacts pulls readable text and structured contacts from any supported message type.
func extractTextAndContacts(msg *waE2E.Message) (string, []SharedContact) {
	if msg.GetConversation() != "" {
		return msg.GetConversation(), nil
	}
	if ext := msg.GetExtendedTextMessage(); ext != nil {
		return ext.GetText(), nil
	}
	if img := msg.GetImageMessage(); img != nil {
		return img.GetCaption(), nil
	}
	if vid := msg.GetVideoMessage(); vid != nil {
		return vid.GetCaption(), nil
	}
	if contact := msg.GetContactMessage(); contact != nil {
		c := parseContact(contact.GetDisplayName(), contact.GetVcard())
		text := formatContact(c.DisplayName, c.Phone)
		return text, []SharedContact{c}
	}
	if contacts := msg.GetContactsArrayMessage(); contacts != nil {
		var parsed []SharedContact
		var parts []string
		for _, c := range contacts.GetContacts() {
			sc := parseContact(c.GetDisplayName(), c.GetVcard())
			parsed = append(parsed, sc)
			parts = append(parts, formatContact(sc.DisplayName, sc.Phone))
		}
		header := "Shared contacts:"
		if name := contacts.GetDisplayName(); name != "" {
			header = fmt.Sprintf("Shared contacts (%s):", name)
		}
		return header + "\n" + strings.Join(parts, "\n"), parsed
	}
	return "", nil
}

func parseContact(displayName, vcard string) SharedContact {
	var phone string
	for _, line := range strings.Split(vcard, "\n") {
		if strings.HasPrefix(line, "TEL") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 && parts[1] != "" {
				phone = parts[1]
				break
			}
		}
	}
	return SharedContact{
		DisplayName: displayName,
		Vcard:       vcard,
		Phone:       phone,
	}
}

func formatContact(displayName, phone string) string {
	if phone != "" {
		return fmt.Sprintf("[Contact] %s (%s)", displayName, phone)
	}
	return fmt.Sprintf("[Contact] %s", displayName)
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

func (c *Client) handleJoinedGroup(evt *events.JoinedGroup) {
	groupName := evt.Name
	var participants []GroupParticipantInfo
	for _, p := range evt.Participants {
		resolvedJID := c.ResolveLID(p.JID)
		participants = append(participants, GroupParticipantInfo{
			JID:          resolvedJID.String(),
			DisplayName:  p.DisplayName,
			IsAdmin:      p.IsAdmin,
			IsSuperAdmin: p.IsSuperAdmin,
		})
	}

	description := ""
	if evt.Topic != "" {
		description = evt.Topic
	}

	if c.onEvent != nil {
		c.onEvent(GroupSyncEvent{
			GroupJID:     evt.JID.String(),
			GroupName:    groupName,
			Description:  description,
			Participants: participants,
			Timestamp:    evt.GroupCreated.UTC().Format("2006-01-02T15:04:05.000Z"),
		})
	}
}

func (c *Client) handleGroupInfo(evt *events.GroupInfo) {
	if len(evt.Join) == 0 && len(evt.Leave) == 0 {
		return
	}

	var joined []string
	for _, jid := range evt.Join {
		resolved := c.ResolveLID(jid)
		joined = append(joined, resolved.String())
	}

	var left []string
	for _, jid := range evt.Leave {
		resolved := c.ResolveLID(jid)
		left = append(left, resolved.String())
	}

	senderJID := ""
	if evt.Sender != nil {
		resolved := c.ResolveLID(*evt.Sender)
		senderJID = resolved.String()
	}

	groupName := ""
	if evt.Name != nil {
		groupName = evt.Name.Name
	}

	if c.onEvent != nil {
		c.onEvent(GroupMemberChangeEvent{
			GroupJID:   evt.JID.String(),
			GroupName:  groupName,
			SenderJID:  senderJID,
			JoinedJIDs: joined,
			LeftJIDs:   left,
			Timestamp:  evt.Timestamp.UTC().Format("2006-01-02T15:04:05.000Z"),
		})
	}
}

// SyncGroups fetches all joined groups and emits GroupSyncEvent for each.
func (c *Client) SyncGroups() {
	groups, err := c.client.GetJoinedGroups(context.Background())
	if err != nil {
		c.log.Warn("failed to fetch joined groups", "error", err)
		return
	}
	c.log.Info("fetched joined groups", "count", len(groups))
	for _, g := range groups {
		var participants []GroupParticipantInfo
		for _, p := range g.Participants {
			resolvedJID := c.ResolveLID(p.JID)
			participants = append(participants, GroupParticipantInfo{
				JID:          resolvedJID.String(),
				DisplayName:  p.DisplayName,
				IsAdmin:      p.IsAdmin,
				IsSuperAdmin: p.IsSuperAdmin,
			})
		}
		description := ""
		if g.Topic != "" {
			description = g.Topic
		}
		if c.onEvent != nil {
			c.onEvent(GroupSyncEvent{
				GroupJID:     g.JID.String(),
				GroupName:    g.Name,
				Description:  description,
				Participants: participants,
				Timestamp:    g.GroupCreated.UTC().Format("2006-01-02T15:04:05.000Z"),
			})
		}
		// Auto-subscribe to presence for all groups
		if err := c.client.SubscribePresence(context.Background(), g.JID); err != nil {
			c.log.Warn("failed to subscribe presence for group", "jid", g.JID.String(), "error", err)
		}
	}
}

func (c *Client) SubscribePresence(chatJID string) error {
	jid, err := types.ParseJID(chatJID)
	if err != nil {
		return fmt.Errorf("parse jid: %w", err)
	}
	return c.client.SubscribePresence(context.Background(), jid)
}

func (c *Client) Close() error {
	c.client.Disconnect()
	return c.container.Close()
}

func gifToMP4(gifData []byte) ([]byte, error) {
	inFile, err := os.CreateTemp("", "gif-*.gif")
	if err != nil {
		return nil, fmt.Errorf("create temp input: %w", err)
	}
	defer os.Remove(inFile.Name())

	if _, err := inFile.Write(gifData); err != nil {
		inFile.Close()
		return nil, fmt.Errorf("write temp input: %w", err)
	}
	inFile.Close()

	outFile, err := os.CreateTemp("", "gif-*.mp4")
	if err != nil {
		return nil, fmt.Errorf("create temp output: %w", err)
	}
	outPath := outFile.Name()
	outFile.Close()
	defer os.Remove(outPath)

	cmd := exec.Command("/home/linuxbrew/.linuxbrew/bin/ffmpeg",
		"-y",
		"-i", inFile.Name(),
		"-c:v", "libx264",
		"-preset", "fast",
		"-crf", "23",
		"-pix_fmt", "yuv420p",
		"-movflags", "+faststart",
		"-an",
		outPath,
	)
	if output, err := cmd.CombinedOutput(); err != nil {
		return nil, fmt.Errorf("ffmpeg: %w: %s", err, string(output))
	}

	return os.ReadFile(outPath)
}
