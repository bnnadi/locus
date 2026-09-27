import test from 'node:test';
import assert from 'node:assert/strict';

import {
  chatCompletionsBaseUrl,
  completionUrl,
  chatRequest,
  resolveServiceBaseUrl,
} from '../src/openai-chat.ts';

// -----------------------------------------------------------------------
// chatCompletionsBaseUrl
// -----------------------------------------------------------------------

test('chatCompletionsBaseUrl: bare host without path gets /v1 appended', () => {
  assert.equal(chatCompletionsBaseUrl('http://hermes:8642'), 'http://hermes:8642/v1');
});

test('chatCompletionsBaseUrl: trailing slash on bare host still yields /v1', () => {
  assert.equal(chatCompletionsBaseUrl('http://hermes:8642/'), 'http://hermes:8642/v1');
});

test('chatCompletionsBaseUrl: already-/v1 base is left untouched', () => {
  assert.equal(chatCompletionsBaseUrl('http://hermes:8642/v1'), 'http://hermes:8642/v1');
});

test('chatCompletionsBaseUrl: /v1 with trailing slash is normalized without trailing slash', () => {
  assert.equal(chatCompletionsBaseUrl('http://hermes:8642/v1/'), 'http://hermes:8642/v1');
});

test('chatCompletionsBaseUrl: ollama host gets /v1 appended', () => {
  assert.equal(chatCompletionsBaseUrl('http://ollama:8080'), 'http://ollama:8080/v1');
});

test('chatCompletionsBaseUrl: huggingface router URL that already ends in /v1 is untouched', () => {
  assert.equal(
    chatCompletionsBaseUrl('https://router.huggingface.co/v1'),
    'https://router.huggingface.co/v1'
  );
});

test('chatCompletionsBaseUrl: does not rewrite ollama default port 11434 to 8080', () => {
  assert.equal(chatCompletionsBaseUrl('http://ollama:11434'), 'http://ollama:11434/v1');
});

test('chatCompletionsBaseUrl: strips trailing /chat/completions before applying the /v1 rule', () => {
  assert.equal(
    chatCompletionsBaseUrl('http://hermes:8642/v1/chat/completions'),
    'http://hermes:8642/v1'
  );
});

test('chatCompletionsBaseUrl: strips a bare (no /v1) trailing /chat/completions segment', () => {
  assert.equal(
    chatCompletionsBaseUrl('http://hermes:8642/chat/completions'),
    'http://hermes:8642/v1'
  );
});

test('chatCompletionsBaseUrl: strips /v1/chat/completions even with a trailing slash', () => {
  assert.equal(
    chatCompletionsBaseUrl('http://hermes:8642/v1/chat/completions/'),
    'http://hermes:8642/v1'
  );
});

// -----------------------------------------------------------------------
// completionUrl
// -----------------------------------------------------------------------

test('completionUrl: appends /chat/completions exactly once onto a bare host', () => {
  assert.equal(completionUrl('http://hermes:8642'), 'http://hermes:8642/v1/chat/completions');
});

test('completionUrl: does not double-append when input already ends in /chat/completions', () => {
  assert.equal(
    completionUrl('http://hermes:8642/v1/chat/completions'),
    'http://hermes:8642/v1/chat/completions'
  );
});

test('completionUrl: appends /chat/completions exactly once onto an already-/v1 base (no /v1/v1)', () => {
  assert.equal(
    completionUrl('http://hermes:8642/v1'),
    'http://hermes:8642/v1/chat/completions'
  );
});

// -----------------------------------------------------------------------
// chatRequest
// -----------------------------------------------------------------------

const fixedMessages = [{ role: 'user', content: 'hello' }];

test('chatRequest: hermes url is the full /v1/chat/completions completion URL', () => {
  const result = chatRequest({
    provider: 'hermes',
    baseUrl: 'http://hermes:8642',
    apiKey: 'test-api-server-key',
    model: 'hermes-agent',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.equal(result.url, 'http://hermes:8642/v1/chat/completions');
});

test('chatRequest: ollama url stops at the /v1 base, not the full chat/completions path', () => {
  const result = chatRequest({
    provider: 'ollama',
    baseUrl: 'http://ollama:11434',
    model: 'llama3',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.equal(result.url, 'http://ollama:11434/v1');
});

test('chatRequest: huggingface url stops at the /v1 base, not the full chat/completions path', () => {
  const result = chatRequest({
    provider: 'huggingface',
    baseUrl: 'https://router.huggingface.co/v1',
    apiKey: 'hf-test-token',
    model: 'meta-llama/Llama-3.2-3B-Instruct:cheapest',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.equal(result.url, 'https://router.huggingface.co/v1');
});

test('chatRequest: hermes body deepEquals { model, messages, temperature } for a fixed input', () => {
  const result = chatRequest({
    provider: 'hermes',
    baseUrl: 'http://hermes:8642',
    apiKey: 'test-api-server-key',
    model: 'hermes-agent',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.deepEqual(result.body, {
    model: 'hermes-agent',
    messages: fixedMessages,
    temperature: 0.7,
  });
});

test('chatRequest: ollama body deepEquals { model, messages, temperature } for a fixed input', () => {
  const result = chatRequest({
    provider: 'ollama',
    baseUrl: 'http://ollama:11434',
    model: 'llama3',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.deepEqual(result.body, {
    model: 'llama3',
    messages: fixedMessages,
    temperature: 0.7,
  });
});

test('chatRequest: huggingface body deepEquals { model, messages, temperature } for a fixed input', () => {
  const result = chatRequest({
    provider: 'huggingface',
    baseUrl: 'https://router.huggingface.co/v1',
    apiKey: 'hf-test-token',
    model: 'meta-llama/Llama-3.2-3B-Instruct:cheapest',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.deepEqual(result.body, {
    model: 'meta-llama/Llama-3.2-3B-Instruct:cheapest',
    messages: fixedMessages,
    temperature: 0.7,
  });
});

test('chatRequest: ollama never sends an Authorization header, even when an apiKey is supplied', () => {
  const result = chatRequest({
    provider: 'ollama',
    baseUrl: 'http://ollama:11434',
    apiKey: 'should-not-be-sent',
    model: 'llama3',
    messages: fixedMessages,
    temperature: 0.7,
  });
  const authorizationHeaderNames = Object.keys(result.headers).filter((name) =>
    /^authorization$/i.test(name)
  );
  assert.deepEqual(
    authorizationHeaderNames,
    [],
    'headers must not contain any authorization header (case-insensitive), regardless of value'
  );
});

test('chatRequest: huggingface Authorization header is Bearer <apiKey>', () => {
  const result = chatRequest({
    provider: 'huggingface',
    baseUrl: 'https://router.huggingface.co/v1',
    apiKey: 'hf-test-token',
    model: 'meta-llama/Llama-3.2-3B-Instruct:cheapest',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.equal(result.headers.Authorization, 'Bearer hf-test-token');
});

test('chatRequest: hermes Authorization header is Bearer <apiKey>', () => {
  const result = chatRequest({
    provider: 'hermes',
    baseUrl: 'http://hermes:8642',
    apiKey: 'test-api-server-key',
    model: 'hermes-agent',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.equal(result.headers.Authorization, 'Bearer test-api-server-key');
});

test('chatRequest: useResponsesApi is exactly false for hermes', () => {
  const result = chatRequest({
    provider: 'hermes',
    baseUrl: 'http://hermes:8642',
    apiKey: 'test-api-server-key',
    model: 'hermes-agent',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.equal(result.useResponsesApi, false);
});

test('chatRequest: useResponsesApi is exactly false for ollama', () => {
  const result = chatRequest({
    provider: 'ollama',
    baseUrl: 'http://ollama:11434',
    model: 'llama3',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.equal(result.useResponsesApi, false);
});

test('chatRequest: useResponsesApi is exactly false for huggingface', () => {
  const result = chatRequest({
    provider: 'huggingface',
    baseUrl: 'https://router.huggingface.co/v1',
    apiKey: 'hf-test-token',
    model: 'meta-llama/Llama-3.2-3B-Instruct:cheapest',
    messages: fixedMessages,
    temperature: 0.7,
  });
  assert.equal(result.useResponsesApi, false);
});

// -----------------------------------------------------------------------
// resolveServiceBaseUrl
// -----------------------------------------------------------------------

test('resolveServiceBaseUrl: huggingface ignores credential and env, always returns router.huggingface.co/v1', () => {
  const result = resolveServiceBaseUrl('huggingface', 'http://ignored:1234', {
    HERMES_URL: 'http://ignored-too:9999',
    OLLAMA_URL: 'http://also-ignored:9999',
  });
  assert.equal(result, 'https://router.huggingface.co/v1');
});

test('resolveServiceBaseUrl: huggingface returns router.huggingface.co/v1 even with empty credential and empty env', () => {
  const result = resolveServiceBaseUrl('huggingface', '', {});
  assert.equal(result, 'https://router.huggingface.co/v1');
});

test('resolveServiceBaseUrl: hermes non-empty credential wins over env, normalized to /v1', () => {
  const result = resolveServiceBaseUrl('hermes', 'http://hermes:8642', {
    HERMES_URL: 'http://should-not-be-used:9999',
  });
  assert.equal(result, 'http://hermes:8642/v1');
});

test('resolveServiceBaseUrl: hermes empty credential falls back to env.HERMES_URL, normalized', () => {
  const result = resolveServiceBaseUrl('hermes', '', {
    HERMES_URL: 'http://hermes:8642',
  });
  assert.equal(result, 'http://hermes:8642/v1');
});

test('resolveServiceBaseUrl: ollama non-empty credential wins over env, normalized to /v1', () => {
  const result = resolveServiceBaseUrl('ollama', 'http://ollama:11434', {
    OLLAMA_URL: 'http://should-not-be-used:9999',
  });
  assert.equal(result, 'http://ollama:11434/v1');
});

test('resolveServiceBaseUrl: ollama empty credential falls back to env.OLLAMA_URL, normalized', () => {
  const result = resolveServiceBaseUrl('ollama', '', {
    OLLAMA_URL: 'http://ollama:11434',
  });
  assert.equal(result, 'http://ollama:11434/v1');
});

test('resolveServiceBaseUrl: hermes throws with HERMES_URL in the message when credential empty and env missing', () => {
  assert.throws(
    () => resolveServiceBaseUrl('hermes', '', {}),
    (err: unknown) => err instanceof Error && err.message.includes('HERMES_URL')
  );
});

test('resolveServiceBaseUrl: hermes must not silently fall back to a baked-in hermes host/port (error names HERMES_URL)', () => {
  assert.throws(
    () => resolveServiceBaseUrl('hermes', '', { HERMES_URL: undefined }),
    (err: unknown) => err instanceof Error && err.message.includes('HERMES_URL')
  );
});

test('resolveServiceBaseUrl: hermes with empty-string HERMES_URL env still throws naming HERMES_URL', () => {
  assert.throws(
    () => resolveServiceBaseUrl('hermes', '', { HERMES_URL: '' }),
    (err: unknown) => err instanceof Error && err.message.includes('HERMES_URL')
  );
});

test('resolveServiceBaseUrl: ollama throws with OLLAMA_URL in the message when credential empty and env missing', () => {
  assert.throws(
    () => resolveServiceBaseUrl('ollama', '', {}),
    (err: unknown) => err instanceof Error && err.message.includes('OLLAMA_URL')
  );
});

test('resolveServiceBaseUrl: ollama must not silently fall back to a baked-in ollama host/port (error names OLLAMA_URL)', () => {
  assert.throws(
    () => resolveServiceBaseUrl('ollama', '', { OLLAMA_URL: undefined }),
    (err: unknown) => err instanceof Error && err.message.includes('OLLAMA_URL')
  );
});

test('resolveServiceBaseUrl: ollama with empty-string OLLAMA_URL env still throws naming OLLAMA_URL', () => {
  assert.throws(
    () => resolveServiceBaseUrl('ollama', '', { OLLAMA_URL: '' }),
    (err: unknown) => err instanceof Error && err.message.includes('OLLAMA_URL')
  );
});

test('resolveServiceBaseUrl: explicit 127.0.0.1 credential override is honored and normalized', () => {
  const result = resolveServiceBaseUrl('hermes', 'http://127.0.0.1:8642', {
    HERMES_URL: 'http://should-not-be-used:9999',
  });
  assert.equal(result, 'http://127.0.0.1:8642/v1');
});
