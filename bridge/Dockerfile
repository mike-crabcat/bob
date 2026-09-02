# WhatsApp bridge — Go build → minimal runtime.
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /whatsappbridge ./cmd/whatsappbridge

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m bob
COPY --from=build /whatsappbridge /usr/local/bin/whatsappbridge
USER bob
ENTRYPOINT ["/usr/local/bin/whatsappbridge"]
