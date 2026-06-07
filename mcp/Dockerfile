# mykeyvault-mcp — HTTP-Transport (StreamableHTTP) Container.
# Multi-Stage: Build mit devDependencies (tsc), Runtime nur mit prod-deps.
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:22-slim
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist

ENV MCP_TRANSPORT=http
ENV HOST=0.0.0.0
ENV PORT=3458
EXPOSE 3458

CMD ["node", "dist/index.js"]
