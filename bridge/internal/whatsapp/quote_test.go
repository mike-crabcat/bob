package whatsapp

import (
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
)

func strPtr(s string) *string { return &s }

func boolPtr(b bool) *bool { return &b }

func TestContextInfoOf(t *testing.T) {
	ci := &waE2E.ContextInfo{StanzaID: strPtr("QUOTED-1")}

	tests := []struct {
		name string
		msg  *waE2E.Message
		want *waE2E.ContextInfo
	}{
		{
			name: "extended text carries context info",
			msg: &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
				Text:        strPtr("a reply"),
				ContextInfo: ci,
			}},
			want: ci,
		},
		{
			name: "image reply carries context info on the image",
			msg: &waE2E.Message{ImageMessage: &waE2E.ImageMessage{
				Caption:     strPtr("look at this"),
				ContextInfo: ci,
			}},
			want: ci,
		},
		{
			name: "video reply carries context info on the video",
			msg: &waE2E.Message{VideoMessage: &waE2E.VideoMessage{
				ContextInfo: ci,
			}},
			want: ci,
		},
		{
			name: "document reply carries context info on the document",
			msg: &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
				FileName:    strPtr("menu.pdf"),
				ContextInfo: ci,
			}},
			want: ci,
		},
		{
			name: "audio reply carries context info",
			msg: &waE2E.Message{AudioMessage: &waE2E.AudioMessage{
				ContextInfo: ci,
			}},
			want: ci,
		},
		{
			name: "plain conversation has none",
			msg:  &waE2E.Message{Conversation: strPtr("no reply here")},
			want: nil,
		},
		{
			name: "extended text without context info",
			msg: &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
				Text: strPtr("just a link message"),
			}},
			want: nil,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := contextInfoOf(tc.msg)
			if got != tc.want {
				t.Fatalf("contextInfoOf() = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestQuotedPreview(t *testing.T) {
	tests := []struct {
		name string
		qm   *waE2E.Message
		want string
	}{
		{name: "nil snapshot", qm: nil, want: ""},
		{
			name: "plain text quote",
			qm:   &waE2E.Message{Conversation: strPtr("are we still on for friday?")},
			want: "are we still on for friday?",
		},
		{
			name: "extended text quote",
			qm: &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
				Text: strPtr("the long version"),
			}},
			want: "the long version",
		},
		{
			name: "image quote with caption",
			qm: &waE2E.Message{ImageMessage: &waE2E.ImageMessage{
				Caption: strPtr("the venue"),
			}},
			want: "the venue",
		},
		{name: "image quote without caption", qm: &waE2E.Message{ImageMessage: &waE2E.ImageMessage{}}, want: "an image"},
		{name: "video quote", qm: &waE2E.Message{VideoMessage: &waE2E.VideoMessage{}}, want: "a video"},
		{
			name: "gif quote",
			qm: &waE2E.Message{VideoMessage: &waE2E.VideoMessage{
				GifPlayback: boolPtr(true),
			}},
			want: "a GIF",
		},
		{name: "audio quote", qm: &waE2E.Message{AudioMessage: &waE2E.AudioMessage{}}, want: "an audio message"},
		{
			name: "document quote with filename",
			qm: &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
				FileName: strPtr("setlist.pdf"),
			}},
			want: "a document: setlist.pdf",
		},
		{name: "document quote without filename", qm: &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{}}, want: "a document"},
		{name: "sticker quote", qm: &waE2E.Message{StickerMessage: &waE2E.StickerMessage{}}, want: "a sticker"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := quotedPreview(tc.qm); got != tc.want {
				t.Fatalf("quotedPreview() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestQuotedPreviewTruncates(t *testing.T) {
	long := strings.Repeat("x", quotedPreviewMax+50)
	got := quotedPreview(&waE2E.Message{Conversation: strPtr(long)})
	if got != strings.Repeat("x", quotedPreviewMax)+"…" {
		t.Fatalf("truncation to %d runes + ellipsis failed (len=%d)", quotedPreviewMax, len(got))
	}

	// Multi-byte runes must not be split mid-rune.
	emoji := strings.Repeat("🎉", 250) // 250 runes, 1000 bytes
	got = quotedPreview(&waE2E.Message{Conversation: strPtr(emoji)})
	want := strings.Repeat("🎉", quotedPreviewMax) + "…"
	if got != want {
		t.Fatalf("rune-safe truncation failed: got %d bytes, want %d bytes", len(got), len(want))
	}
}

func TestTruncateRunes(t *testing.T) {
	if got := truncateRunes("short", 10); got != "short" {
		t.Fatalf("under limit should be unchanged, got %q", got)
	}
	if got := truncateRunes("", 10); got != "" {
		t.Fatalf("empty string should stay empty, got %q", got)
	}
	if got := truncateRunes("abcdef", 3); got != "abc…" {
		t.Fatalf("got %q", got)
	}
	if got := truncateRunes(strings.Repeat("é", 5), 3); got != "ééé…" {
		t.Fatalf("rune boundary split: got %q", got)
	}
}
