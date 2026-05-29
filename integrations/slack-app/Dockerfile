# SPDX-License-Identifier: Apache-2.0
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev --no-audit --no-fund

FROM node:20-alpine
RUN addgroup -S plynf && adduser -S plynf -G plynf
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY package.json ./
COPY src ./src
USER plynf
ENV NODE_ENV=production PORT=3000
EXPOSE 3000
CMD ["node", "src/app.js"]
