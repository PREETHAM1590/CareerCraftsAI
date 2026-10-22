FROM careercraft-computer:b6932d3
COPY harden-computer.mjs /tmp/harden-computer.mjs
RUN bun /tmp/harden-computer.mjs /app/src/index.ts && rm /tmp/harden-computer.mjs
# Chromium renders the open internet with --no-sandbox (gVisor is underneath),
# so it must not also run as root. New volumes take this ownership from the
# image; volumes created before this need a one-time chown (see README).
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin computer  && mkdir -p /profiles /workspace  && chown -R 10001:10001 /profiles /workspace
USER 10001:10001
ENV HOME=/home/computer
