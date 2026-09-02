BINARY  := bin/whatsappbridge
SRC     := $(shell find . -name '*.go' -not -path './vendor/*')

.PHONY: build test install clean

build:
	go build -o $(BINARY) ./cmd/whatsappbridge

test:
	go test ./... -v

install: build
	cp $(BINARY) $(HOME)/.local/bin/whatsappbridge

clean:
	rm -rf bin/
