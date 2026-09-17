package whatsapp

import (
	"encoding/json"
	"io"
	"log/slog"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"go.bob.dev/whatsappbridge/internal/wsproto"
)

// newReactionTestClient builds the minimal Client handleReaction needs: a
// whatsmeow client with an empty device store (LID store nil → ResolveLID
// passes JIDs through) and a discarding logger.
func newReactionTestClient(t *testing.T) *Client {
	t.Helper()
	return &Client{
		log:    slog.New(slog.NewTextHandler(io.Discard, nil)),
		client: whatsmeow.NewClient(&store.Device{}, nil),
	}
}

func reactionMessage(key *waCommon.MessageKey, emoji string) *waE2E.ReactionMessage {
	return &waE2E.ReactionMessage{Key: key, Text: strPtr(emoji)}
}

func reactionInfo() types.MessageInfo {
	return types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     types.NewJID("61490000001", types.DefaultUserServer),
			Sender:   types.NewJID("61490000001", types.DefaultUserServer),
			IsGroup:  false,
			IsFromMe: false,
		},
		ID:        "REACT-1",
		PushName:  "Mike T",
		Timestamp: time.Date(2026, 9, 14, 10, 0, 0, 0, time.UTC),
	}
}

func TestBuildIncomingReactionEvent(t *testing.T) {
	ts := time.Date(2026, 9, 14, 10, 0, 0, 0, time.UTC)

	tests := []struct {
		name string
		info types.MessageInfo
		rx   *waE2E.ReactionMessage
		want IncomingReactionEvent
	}{
		{
			name: "dm reaction",
			info: reactionInfo(),
			rx: reactionMessage(&waCommon.MessageKey{
				RemoteJID: strPtr("61490000001@s.whatsapp.net"),
				ID:        strPtr("TARGET-1"),
			}, "👍"),
			want: IncomingReactionEvent{
				WhatsAppMessageID: "REACT-1",
				ChatKind:          "dm",
				SenderName:        "Mike T",
				TargetMessageID:   "TARGET-1",
				Emoji:             "👍",
				Timestamp:         ts.UTC().Format("2006-01-02T15:04:05.000Z"),
			},
		},
		{
			name: "group reaction",
			info: func() types.MessageInfo {
				info := reactionInfo()
				info.Chat = types.NewJID("12036302", types.GroupServer)
				info.Sender = types.NewJID("61490000002", types.DefaultUserServer)
				info.IsGroup = true
				return info
			}(),
			rx: reactionMessage(&waCommon.MessageKey{
				RemoteJID:   strPtr("12036302@g.us"),
				ID:          strPtr("TARGET-2"),
				Participant: strPtr("61490000003@s.whatsapp.net"),
			}, "❤️"),
			want: IncomingReactionEvent{
				WhatsAppMessageID: "REACT-1",
				ChatKind:          "group",
				SenderName:        "Mike T",
				TargetMessageID:   "TARGET-2",
				Emoji:             "❤️",
				Timestamp:         ts.UTC().Format("2006-01-02T15:04:05.000Z"),
			},
		},
		{
			name: "removal is empty emoji",
			info: reactionInfo(),
			rx: reactionMessage(&waCommon.MessageKey{
				RemoteJID: strPtr("61490000001@s.whatsapp.net"),
				ID:        strPtr("TARGET-1"),
			}, ""),
			want: IncomingReactionEvent{
				WhatsAppMessageID: "REACT-1",
				ChatKind:          "dm",
				SenderName:        "Mike T",
				TargetMessageID:   "TARGET-1",
				Emoji:             "",
				Timestamp:         ts.UTC().Format("2006-01-02T15:04:05.000Z"),
			},
		},
		{
			// Nil Key is dropped by handleReaction's guard (see
			// TestHandleReactionGuards); the pure builder just reports an
			// empty target so the guard has something to reject on.
			name: "nil key yields empty target",
			info: reactionInfo(),
			rx:   reactionMessage(nil, "👍"),
			want: IncomingReactionEvent{
				WhatsAppMessageID: "REACT-1",
				ChatKind:          "dm",
				SenderName:        "Mike T",
				Emoji:             "👍",
				Timestamp:         ts.UTC().Format("2006-01-02T15:04:05.000Z"),
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := buildIncomingReactionEvent(tc.info, tc.rx)
			if got != tc.want {
				t.Fatalf("buildIncomingReactionEvent() = %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestHandleReactionGuards(t *testing.T) {
	// FromMe (Bob reacting from his own phone) must not emit an event —
	// feeding it back would self-loop.
	evt := &events.Message{
		Info: reactionInfo(),
		Message: &waE2E.Message{ReactionMessage: reactionMessage(&waCommon.MessageKey{
			RemoteJID: strPtr("61490000001@s.whatsapp.net"),
			ID:        strPtr("TARGET-1"),
		}, "👍")},
	}
	evt.Info.IsFromMe = true

	c := newReactionTestClient(t)
	var emitted []IncomingReactionEvent
	c.onEvent = func(e any) {
		if r, ok := e.(IncomingReactionEvent); ok {
			emitted = append(emitted, r)
		}
	}
	c.handleReaction(evt, evt.Message.GetReactionMessage())
	if len(emitted) != 0 {
		t.Fatalf("FromMe reaction should be dropped, got %d events", len(emitted))
	}

	// Missing target id must not emit either.
	evt.Info.IsFromMe = false
	evt.Message = &waE2E.Message{ReactionMessage: reactionMessage(&waCommon.MessageKey{}, "👍")}
	c.handleReaction(evt, evt.Message.GetReactionMessage())
	if len(emitted) != 0 {
		t.Fatalf("keyless reaction should be dropped, got %d events", len(emitted))
	}
}

func TestHandleReactionResolvesTargetSender(t *testing.T) {
	// Group reaction: Key.Participant identifies the target's author.
	evt := &events.Message{
		Info: func() types.MessageInfo {
			info := reactionInfo()
			info.Chat = types.NewJID("12036302", types.GroupServer)
			info.Sender = types.NewJID("61490000002", types.DefaultUserServer)
			info.IsGroup = true
			return info
		}(),
		Message: &waE2E.Message{ReactionMessage: reactionMessage(&waCommon.MessageKey{
			RemoteJID:   strPtr("12036302@g.us"),
			ID:          strPtr("TARGET-2"),
			Participant: strPtr("61490000003@s.whatsapp.net"),
		}, "👍")},
	}

	c := newReactionTestClient(t)
	var got IncomingReactionEvent
	c.onEvent = func(e any) {
		if r, ok := e.(IncomingReactionEvent); ok {
			got = r
		}
	}
	c.handleReaction(evt, evt.Message.GetReactionMessage())

	if got.TargetSenderJID != "61490000003@s.whatsapp.net" {
		t.Fatalf("group target sender = %q, want the participant JID", got.TargetSenderJID)
	}
	if got.ChatID != "12036302@g.us" || got.SenderJID != "61490000002@s.whatsapp.net" {
		t.Fatalf("chat/sender resolution failed: chat=%q sender=%q", got.ChatID, got.SenderJID)
	}

	// DM reaction: no Participant — falls back to RemoteJID (the peer).
	evt.Info.Chat = types.NewJID("61490000001", types.DefaultUserServer)
	evt.Info.Sender = types.NewJID("61490000001", types.DefaultUserServer)
	evt.Info.IsGroup = false
	evt.Message = &waE2E.Message{ReactionMessage: reactionMessage(&waCommon.MessageKey{
		RemoteJID: strPtr("61490000001@s.whatsapp.net"),
		ID:        strPtr("TARGET-1"),
	}, "👍")}
	c.handleReaction(evt, evt.Message.GetReactionMessage())
	if got.TargetSenderJID != "61490000001@s.whatsapp.net" {
		t.Fatalf("dm target sender = %q, want the RemoteJID fallback", got.TargetSenderJID)
	}
}

func TestSendReactionPayloadRoundTrip(t *testing.T) {
	payload := wsproto.SendReactionPayload{
		ChatID:          "12036302@g.us",
		TargetMessageID: "TARGET-9",
		Emoji:           "👍",
		RequestID:       "req-1",
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var back wsproto.SendReactionPayload
	if err := json.Unmarshal(raw, &back); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if back != payload {
		t.Fatalf("round trip = %+v, want %+v", back, payload)
	}

	// Emoji must survive as-is (multi-byte, no omitempty surprises).
	var probe map[string]any
	if err := json.Unmarshal(raw, &probe); err != nil {
		t.Fatalf("probe unmarshal: %v", err)
	}
	if probe["emoji"] != "👍" {
		t.Fatalf("emoji round trip = %v", probe["emoji"])
	}
	// Empty target sender is omitted from the wire payload.
	if _, present := probe["target_sender_jid"]; present {
		t.Fatalf("empty target_sender_jid should be omitted, got %v", probe["target_sender_jid"])
	}
}
