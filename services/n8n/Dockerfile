FROM n8nio/n8n:2.34.6

# Entrypoint must start as root to chown the Railway volume, then drops
# to node before exec'ing n8n. See entrypoint.sh.
USER root
COPY entrypoint.sh /locus-entrypoint.sh
RUN chmod +x /locus-entrypoint.sh

ENTRYPOINT ["tini", "--", "/locus-entrypoint.sh"]
