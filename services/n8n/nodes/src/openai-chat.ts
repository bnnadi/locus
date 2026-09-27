export type Provider = 'hermes' | 'ollama' | 'huggingface';

export interface ChatMessage {
  role: string;
  content: string;
}

export interface ChatRequestInput {
  provider: Provider;
  baseUrl: string;
  apiKey?: string;
  model: string;
  messages: ChatMessage[];
  temperature: number;
}

export interface ChatRequestOutput {
  url: string;
  body: {
    model: string;
    messages: unknown;
    temperature: number;
  };
  headers: Record<string, string>;
  useResponsesApi: false;
}

const CHAT_COMPLETIONS_SUFFIX = '/chat/completions';
const HUGGINGFACE_ROUTER_BASE = 'https://router.huggingface.co/v1';

function stripTrailingSlashes(value: string): string {
  let url = value;
  while (url.endsWith('/')) {
    url = url.slice(0, -1);
  }
  return url;
}

/**
 * Base URL the OpenAI client expects. The client appends `/chat/completions`
 * itself, so this stops at `/v1`.
 */
export function chatCompletionsBaseUrl(input: string): string {
  let url = stripTrailingSlashes(input.trim());
  if (url.endsWith(CHAT_COMPLETIONS_SUFFIX)) {
    url = stripTrailingSlashes(url.slice(0, -CHAT_COMPLETIONS_SUFFIX.length));
  }
  if (!url.endsWith('/v1')) {
    url = `${url}/v1`;
  }
  return url;
}

/** Full chat-completions URL for the Hermes one-shot call. Appended once. */
export function completionUrl(input: string): string {
  return `${chatCompletionsBaseUrl(input)}${CHAT_COMPLETIONS_SUFFIX}`;
}

export function chatRequest(input: ChatRequestInput): ChatRequestOutput {
  const base = chatCompletionsBaseUrl(input.baseUrl);
  const url = input.provider === 'hermes' ? `${base}${CHAT_COMPLETIONS_SUFFIX}` : base;
  const headers: Record<string, string> = {};
  if (input.provider !== 'ollama' && input.apiKey) {
    headers.Authorization = `Bearer ${input.apiKey}`;
  }
  return {
    url,
    body: {
      model: input.model,
      messages: input.messages,
      temperature: input.temperature,
    },
    headers,
    useResponsesApi: false,
  };
}

function requiredEnv(name: string, env: Record<string, string | undefined>): string {
  const value = env[name];
  if (value === undefined || value.trim() === '') {
    throw new Error(`${name} is required when the credential base URL is empty`);
  }
  return value;
}

export function resolveServiceBaseUrl(
  provider: Provider,
  credentialBaseUrl: string,
  env: Record<string, string | undefined>,
): string {
  if (provider === 'huggingface') {
    return HUGGINGFACE_ROUTER_BASE;
  }
  const override = credentialBaseUrl.trim();
  if (override !== '') {
    return chatCompletionsBaseUrl(override);
  }
  const envName = provider === 'hermes' ? 'HERMES_URL' : 'OLLAMA_URL';
  return chatCompletionsBaseUrl(requiredEnv(envName, env));
}
