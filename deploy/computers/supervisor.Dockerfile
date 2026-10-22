# Source comes from the exact OpenBot revision in compose.computers.yml.
FROM oven/bun:1.3.14-alpine
WORKDIR /app
COPY --from=openbot supervisor/package.json supervisor/bun.lock ./
RUN bun install --frozen-lockfile
COPY --from=openbot supervisor/src ./src
COPY deploy/computers/LICENSE.openbot ./LICENSE.openbot
COPY deploy/computers/harden-supervisor.mjs /tmp/harden-supervisor.mjs
RUN bun /tmp/harden-supervisor.mjs /app/src && rm /tmp/harden-supervisor.mjs
COPY deploy/computers/purge-supervisor.mjs /tmp/purge-supervisor.mjs
RUN bun /tmp/purge-supervisor.mjs /app/src && rm /tmp/purge-supervisor.mjs
EXPOSE 4300
CMD ["bun", "src/index.ts"]
