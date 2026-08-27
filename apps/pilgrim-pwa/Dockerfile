# Pilgrim PWA — build, verify the budget, serve from nginx.
#
# The budget check runs *inside the image build*, not just in CI. A pilgrim app
# that quietly grew to 400 KB would still deploy if the only gate were a
# workflow somebody can skip; failing the build is the gate that cannot be.

FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run typecheck && npm run build && npm run budget


FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q --spider http://localhost/ || exit 1
