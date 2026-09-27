import {
  BaseChatModel,
  type GenerateResult,
  type Message,
  type StreamChunk,
  type Tool,
} from '@n8n/ai-node-sdk';

import {
  chatRequest,
  completionUrl,
  type ChatMessage,
  type Provider,
} from './openai-chat';

type FetchChatProvider = Extract<Provider, 'ollama' | 'huggingface'>;

interface OpenAiToolCall {
  id?: string;
  function?: { name?: string; arguments?: string };
}

interface OpenAiChatResponse {
  id?: string;
  choices?: Array<{
    finish_reason?: string | null;
    message?: {
      content?: string | null;
      tool_calls?: OpenAiToolCall[];
    };
  }>;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
}

function messageText(message: Message): string {
  return message.content
    .filter((part) => part.type === 'text')
    .map((part) => part.text)
    .join('');
}

function toPlainMessages(messages: Message[]): ChatMessage[] {
  return messages.map((message) => ({
    role: message.role,
    content: messageText(message),
  }));
}

function openAiTools(tools: Tool[]): Array<Record<string, unknown>> {
  const functions = tools.filter((tool) => tool.type === 'function');
  return functions.map((tool) => ({
    type: 'function',
    function: {
      name: tool.name,
      description: tool.description ?? '',
      parameters:
        tool.inputSchema !== null &&
        typeof tool.inputSchema === 'object' &&
        !('safeParse' in tool.inputSchema)
          ? tool.inputSchema
          : { type: 'object', properties: {} },
    },
  }));
}

function finishReason(value: string | null | undefined): GenerateResult['finishReason'] {
  switch (value) {
    case 'stop':
    case 'length':
    case 'content_filter':
      return value === 'content_filter' ? 'content-filter' : value;
    case 'tool_calls':
      return 'tool-calls';
    default:
      return 'other';
  }
}

export class LocusFetchChatModel extends BaseChatModel {
  constructor(
    provider: FetchChatProvider,
    modelId: string,
    private readonly baseUrl: string,
    private readonly apiKey: string | undefined,
    temperature: number,
  ) {
    super(provider, modelId, { temperature });
  }

  async generate(messages: Message[]): Promise<GenerateResult> {
    const temperature = this.defaultConfig?.temperature ?? 0.7;
    const provider = this.provider as FetchChatProvider;
    const request = chatRequest({
      provider,
      baseUrl: this.baseUrl,
      apiKey: provider === 'ollama' ? undefined : this.apiKey,
      model: this.modelId,
      messages: toPlainMessages(messages),
      temperature,
    });
    const tools = openAiTools(this.tools);
    const body = tools.length > 0 ? { ...request.body, tools } : request.body;
    const response = await fetch(completionUrl(this.baseUrl), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...request.headers,
      },
      body: JSON.stringify(body),
    });
    const raw = await response.text();
    if (!response.ok) {
      throw new Error(`Chat completion failed (${response.status}): ${raw.slice(0, 500)}`);
    }
    const parsed = JSON.parse(raw) as OpenAiChatResponse;
    const choice = parsed.choices?.[0];
    const text = choice?.message?.content ?? '';
    const content: Message['content'] = [];
    if (text) {
      content.push({ type: 'text', text });
    }
    for (const call of choice?.message?.tool_calls ?? []) {
      content.push({
        type: 'tool-call',
        toolCallId: call.id,
        toolName: call.function?.name ?? '',
        input: call.function?.arguments ?? '',
      });
    }
    if (content.length === 0) {
      content.push({ type: 'text', text: '' });
    }
    return {
      id: parsed.id,
      finishReason: finishReason(choice?.finish_reason),
      usage: {
        promptTokens: parsed.usage?.prompt_tokens ?? 0,
        completionTokens: parsed.usage?.completion_tokens ?? 0,
        totalTokens: parsed.usage?.total_tokens ?? 0,
      },
      message: { role: 'assistant', content },
    };
  }

  async *stream(messages: Message[]): AsyncIterable<StreamChunk> {
    const result = await this.generate(messages);
    for (const part of result.message.content) {
      if (part.type === 'text' && part.text) {
        yield { type: 'text-delta', delta: part.text };
      }
      if (part.type === 'tool-call') {
        yield {
          type: 'tool-call-delta',
          id: part.toolCallId,
          name: part.toolName,
          argumentsDelta: part.input,
        };
      }
    }
    yield {
      type: 'finish',
      finishReason: result.finishReason ?? 'stop',
      usage: result.usage,
    };
  }
}
