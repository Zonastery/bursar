# syntax=docker/dockerfile:1
# Multi-stage build for the bursar-web service: static binary, distroless,
# non-root runtime image. CI pins the base images by digest (house style);
# keep the tags here readable but expect the workflow to override them.
FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build
ARG VERSION=dev
ARG COMMIT=unknown
ARG DATE=unknown
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY . .
RUN --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build -trimpath \
    -ldflags "-s -w -X github.com/Zonastery/bursar/web/internal/version.Version=${VERSION} -X github.com/Zonastery/bursar/web/internal/version.Commit=${COMMIT} -X github.com/Zonastery/bursar/web/internal/version.Date=${DATE}" \
    -o /out/bursar-web ./cmd/bursar-web

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/bursar-web /usr/local/bin/bursar-web
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/bursar-web"]
