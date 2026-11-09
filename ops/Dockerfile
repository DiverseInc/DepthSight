FROM alpine:3.19

# Minimal deps - just bash + postgresql client + curl + jq
# ca-certificates is REQUIRED, not optional: the smoke test checks that the
# AGPL source offer is served over a certificate that VERIFIES, and curl
# cannot do that without a CA bundle in the image.
RUN apk add --no-cache \
        bash \
        postgresql15-client \
        curl \
        ca-certificates \
        jq \
    && rm -rf /var/cache/apk/* \
    && mkdir -p /opt/ops/state /backups

COPY scripts/ /opt/ops/
COPY loop.sh /opt/ops/loop.sh

# Strip CRLF line endings (Windows -> Linux CRLF breaks shebangs and cron parsers)
# Then chmod +x every script we copied
RUN find /opt/ops -type f -name '*.sh' -exec sed -i 's/\r$//' {} + \
    && chmod +x /opt/ops/*.sh

# Run the scheduler loop in the foreground
CMD ["bash", "/opt/ops/loop.sh"]
