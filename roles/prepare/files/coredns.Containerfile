FROM docker.io/library/golang:1.27.3-alpine3.24 AS builder
ARG COREDNS_VERS=1.15.0
RUN apk add --no-cache build-base git binutils \
 && git clone --depth 1 --branch "v${COREDNS_VERS}" https://github.com/coredns/coredns.git /coredns
WORKDIR /coredns
COPY . /plugin/dockerdiscovery
RUN sed -i '/^hosts:hosts$/i docker:dockerdiscovery' plugin.cfg \
 && go mod edit -replace dockerdiscovery=/plugin/dockerdiscovery \
 && go generate coredns.go \
 && CGO_ENABLED=1 go build -mod=mod -o /usr/local/bin/coredns \
 && strip /usr/local/bin/coredns

FROM docker.io/library/alpine:3.24.3
RUN apk add --no-cache ca-certificates iproute2 curl netcat-openbsd bash
COPY --from=builder /usr/local/bin/coredns /usr/local/bin/coredns
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
