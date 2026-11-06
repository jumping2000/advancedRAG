# Build context: repository root (see compose.yaml)
FROM node:22.23.4-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.29.8-alpine
# entrypoint-tls.sh picks one of the two server templates at every start (REQ-002)
COPY nginx/default.conf.template nginx/tls.conf.template /etc/nginx/server-templates/
COPY --chmod=755 nginx/entrypoint-tls.sh /entrypoint-tls.sh
COPY --from=frontend /frontend/dist /usr/share/nginx/html
ENTRYPOINT ["/entrypoint-tls.sh"]
CMD ["nginx", "-g", "daemon off;"]
