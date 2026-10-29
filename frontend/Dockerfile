FROM node:24-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
ARG NEXT_PUBLIC_API_URL
# Clerk publishable key is inlined into the client bundle at build time.
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
ARG BACKEND_URL=http://backend:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
ENV BACKEND_URL=$BACKEND_URL
COPY . .
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
# next/image writes its optimizer cache here; the node user must own it.
RUN mkdir -p .next/cache && chown -R node:node .next
USER node
CMD ["node", "server.js"]
