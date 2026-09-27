# n8n

Locus nodes are compiled into this image and loaded from
`N8N_CUSTOM_EXTENSIONS=/opt/locus-n8n-nodes/dist`. They are not installed
from the n8n UI. The UI installer writes to the credential volume, which
is outside git.

## Credentials

Create these only after `N8N_ENCRYPTION_KEY` is set. A missing key makes
every saved credential undecryptable on the next empty data directory.

| Credential | Use |
|---|---|
| Locus Authentik (client credentials) | HTTP Request. Grant type is client credentials, client secret in the body. Paste the token URL (`https://<authentik-public-host>/application/o/token/`). No browser redirect. |
| Locus Hermes API | `API_SERVER_KEY`. Not an Authentik token. Hermes does not accept Authentik JWTs. |
| Locus Ollama | Base URL only. No API key is sent. |
| Locus Hugging Face | Token typed into the credential. Optional model override. |

Leave the Hermes and Ollama base URL fields empty. The node reads
`HERMES_URL` and `OLLAMA_URL` from the n8n container at execution time.
Compose sets `http://hermes:8642` and `http://ollama:8080`. Railway sets
`http://hermes.railway.internal:8642` and
`http://ollama.railway.internal:8080`. A value saved on the credential
overrides that. Changing the env var later does not rewrite a base URL
already stored on the credential.

Ollama in this stack listens on port **8080**. Port 11434 will not connect.

## Nodes

- **Locus Ollama Chat Model** and **Locus Hugging Face Chat Model** attach to the AI Agent as language models. Hugging Face calls `https://router.huggingface.co/v1`. The default model is `meta-llama/Llama-3.2-3B-Instruct:cheapest`. `:cheapest` picks a provider; put an explicit provider suffix on the node when you need the same provider again.
- **Locus Hermes** is a one-shot completion (`POST /v1/chat/completions` with `API_SERVER_KEY`). It is not an AI Agent language model. The Hermes API server is itself an agent, with its own profile and tools, so binding it as a tool-calling chat model would hide that. The model string defaults to `hermes-agent` and does not select a shadow-team profile.

The image sets `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`. Workflow expressions and the Code node cannot read `N8N_ENCRYPTION_KEY` or the database password. The Locus nodes still read `HERMES_URL` and `OLLAMA_URL` inside the node process.

## Disable the Locus nodes

Set `N8N_CUSTOM_EXTENSIONS=/opt/locus-n8n-nodes-off` on the service and
restart. That directory is empty and shipped in the image. n8n's own nodes
stay. Unsetting the variable uses the image default and turns the Locus
nodes back on.
