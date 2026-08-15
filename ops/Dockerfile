FROM alpine:3.19

# supercronic = cron daemon designed for containers (no daemonization, foreground)
# postgresql-client = for pg_dump
# bash curl jq = for the smoke test wrapper
# docker-cli = for inspecting the running stack
RUN apk add --no-cache \
        bash \
        postgresql15-client \
        curl \
        jq \
        docker-cli \
        dcron \
    && rm -rf /var/cache/apk/*

# Install supercronic (small cron that runs in foreground)
ARG SUPERCRONIC_VERSION=v0.2.33
RUN curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
        -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

COPY crontab /etc/crontab
COPY scripts/ /opt/ops/
RUN chmod +x /opt/ops/*.sh \
    && mkdir -p /opt/ops/state /backups

# Run in foreground (PID 1) so docker can manage the lifecycle
CMD ["supercronic", "/etc/crontab"]
