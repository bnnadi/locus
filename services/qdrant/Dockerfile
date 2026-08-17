FROM qdrant/qdrant:latest

# Config and API key are supplied via Railway env vars at runtime:
#   QDRANT__SERVICE__API_KEY
#   QDRANT__SERVICE__ENABLE_TLS=false  (Railway terminates TLS at the edge)
#
# Persistent storage should be mounted at /qdrant/storage via a Railway volume.

EXPOSE 6333 6334
