FROM nousresearch/hermes-agent:v2026.8.16

# The profiles under services/hermes/profiles/ shell out to `opencode` for the
# actual work; the Hermes side of each lane is a thin shim. Without this layer
# every worker blocks on every card, so the CLI is part of the image rather
# than something installed into the volume at runtime.
#
# npm rather than the upstream install script: the base image already ships
# node and npm, and it has neither wget nor unzip for that script to use. It
# also keeps a remote shell script out of the build, and opencode-ai resolves
# its platform binary through optionalDependencies, so this is correct on both
# amd64 and arm64.
#
# Keep this default in sync with OPENCODE_VERSION in config/env.example.
# Railway builds this file directly (config/railway.yml), so the ARG default
# is the version that actually ships — nothing overrides it at build time.
# tests/unit/test_hermes_opencode_pin.sh enforces both the pin and the match.
ARG OPENCODE_VERSION=1.18.32
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && opencode --version

CMD [ "gateway", "run" ]
