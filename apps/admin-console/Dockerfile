# Admin console — build static assets, serve them from nginx.
#
# The nginx stage also reverse-proxies /api to the core API, so the browser sees
# a single origin. That is not a convenience: it means the production topology
# is the same one-origin shape the Vite dev proxy gives us locally, and CORS
# never has to be correct for the console to work.

FROM node:22-alpine AS build
WORKDIR /app

# Copy the manifests first so a source-only change does not reinstall.
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY . .
# Typecheck as part of the image build. A console that compiles on a developer's
# machine and not in CI is a console that breaks on the day it is redeployed.
RUN npm run typecheck && npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q --spider http://localhost/ || exit 1
