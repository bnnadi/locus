import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';

import { defaultHermesModel } from '../../src/defaults';
import { nodeShapes } from '../../src/node-shapes';
import { chatRequest, resolveServiceBaseUrl } from '../../src/openai-chat';

const shape = nodeShapes().hermes;

export class LocusHermes implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Locus Hermes',
    name: 'locusHermes',
    icon: 'fa:robot',
    group: ['transform'],
    version: [1],
    description: 'One chat completion against the Hermes API server. Not an AI Agent language model.',
    defaults: { name: 'Locus Hermes' },
    inputs: [NodeConnectionTypes.Main],
    outputs: shape.aiLanguageModel
      ? [NodeConnectionTypes.AiLanguageModel]
      : [NodeConnectionTypes.Main],
    credentials: [{ name: shape.credential, required: true }],
    properties: [
      {
        displayName: 'Model',
        name: 'model',
        type: 'string',
        default: defaultHermesModel(),
        description: 'Sent as the model field. Does not select a Hermes shadow-team profile.',
      },
      {
        displayName: 'Temperature',
        name: 'temperature',
        type: 'number',
        default: 0.7,
        typeOptions: { minValue: 0, maxValue: 2, numberPrecision: 2 },
      },
      {
        displayName: 'Prompt',
        name: 'prompt',
        type: 'string',
        typeOptions: { rows: 4 },
        default: '',
        required: true,
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const out: INodeExecutionData[] = [];
    for (let i = 0; i < items.length; i++) {
      const credentials = await this.getCredentials('locusHermesApi');
      const apiKey = String(credentials.apiKey ?? '');
      if (!apiKey) {
        throw new NodeOperationError(this.getNode(), 'Locus Hermes API key is empty');
      }
      const baseUrl = resolveServiceBaseUrl(
        'hermes',
        String(credentials.baseUrl ?? ''),
        process.env,
      );
      const model = this.getNodeParameter('model', i) as string;
      const temperature = this.getNodeParameter('temperature', i) as number;
      const prompt = this.getNodeParameter('prompt', i) as string;
      const request = chatRequest({
        provider: 'hermes',
        baseUrl,
        apiKey,
        model,
        messages: [{ role: 'user', content: prompt }],
        temperature,
      });
      const response = await this.helpers.httpRequest({
        method: 'POST',
        url: request.url,
        headers: {
          'Content-Type': 'application/json',
          ...request.headers,
        },
        body: request.body,
        json: true,
      });
      const text = response?.choices?.[0]?.message?.content ?? '';
      out.push({ json: { text, model, raw: response } });
    }
    return [out];
  }
}
