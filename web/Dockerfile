# Atlas web image: static Angular build served by nginx on port 8080. `/api/` is proxied to
# the `api` compose service (see nginx.conf).
FROM node:24-slim AS build

WORKDIR /web

COPY package.json package-lock.json ./
RUN npm ci

# Only the sources the build needs; a host `node_modules` must never reach the image.
COPY angular.json tsconfig.json tsconfig.app.json ./
COPY public ./public
COPY src ./src
RUN npx ng build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist/web/browser /usr/share/nginx/html

EXPOSE 8080
