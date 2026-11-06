# Build context: repository root (see compose.yaml)
FROM node:22.23.4-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.29.8-alpine
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=frontend /frontend/dist /usr/share/nginx/html
