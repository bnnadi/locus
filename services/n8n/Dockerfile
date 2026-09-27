FROM node:24.18.0-bookworm-slim AS nodes

WORKDIR /src
COPY nodes/package.json nodes/package-lock.json ./
RUN npm ci
COPY nodes/tsconfig.json ./tsconfig.json
COPY nodes/src ./src
COPY nodes/credentials ./credentials
COPY nodes/nodes ./nodes
COPY nodes/scripts ./scripts
RUN npm run build

FROM n8nio/n8n:2.34.6

# Entrypoint must start as root to chown the Railway volume, then drops
# to node before exec'ing n8n. See entrypoint.sh.
USER root
COPY entrypoint.sh /locus-entrypoint.sh
RUN chmod +x /locus-entrypoint.sh

COPY --from=nodes /src/dist /opt/locus-n8n-nodes/dist
COPY scripts/link-n8n-sdk.js /opt/locus-n8n-nodes/link-n8n-sdk.js
RUN mkdir -p /opt/locus-n8n-nodes-off \
  && chmod 755 /opt/locus-n8n-nodes-off \
  && node /opt/locus-n8n-nodes/link-n8n-sdk.js \
  && chmod -R a+rX /opt/locus-n8n-nodes

# Rollback: set N8N_CUSTOM_EXTENSIONS=/opt/locus-n8n-nodes-off
# n8n 2.34 blocks workflow env access unless this is the string "false".
# Pin true so a Code node or expression cannot read N8N_ENCRYPTION_KEY.
# The Locus nodes still read HERMES_URL and OLLAMA_URL in-process.
ENV N8N_CUSTOM_EXTENSIONS=/opt/locus-n8n-nodes/dist \
    N8N_BLOCK_ENV_ACCESS_IN_NODE=true

ENTRYPOINT ["tini", "--", "/locus-entrypoint.sh"]
