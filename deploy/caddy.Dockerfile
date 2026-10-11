# Caddy with the console baked in. One image serves the whole public face of Nightjar: TLS, the console's static files,
# and a reverse proxy to the hub for the API and the report endpoints.
#
# Built from the repository root (docker-compose.yml does this):  docker build -f deploy/caddy.Dockerfile .

FROM node:24-alpine AS console
WORKDIR /build
# lockfile first, so the install layer is cached until a dependency changes
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
# `vite build` only. The type-check and tests run in CI, not on the server.
RUN npm run build

FROM caddy:2-alpine
COPY --from=console /build/dist /srv/console
COPY deploy/Caddyfile /etc/caddy/Caddyfile
