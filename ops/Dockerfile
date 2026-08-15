FROM alpine:3.19

# Minimal deps - just bash + postgresql client + curl + jq
RUN apk add --no-cache \
        bash \
        postgresql15-client \
        curl \
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
