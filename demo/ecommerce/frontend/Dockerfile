# ---- build stage ----
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
# Use npm install (no lockfile committed for this demo); swap to `npm ci` once
# you commit a package-lock.json.
RUN npm install
COPY . .
RUN npm run build

# ---- serve stage ----
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# Entrypoint regenerates /config.js from env vars on every container start,
# so VITE_USER_SERVICE_URL / VITE_ORDER_SERVICE_URL in docker-compose take
# effect at runtime (not just at build time).
COPY docker-entrypoint.sh /docker-entrypoint.d/40-app-config.sh
RUN chmod +x /docker-entrypoint.d/40-app-config.sh
EXPOSE 80