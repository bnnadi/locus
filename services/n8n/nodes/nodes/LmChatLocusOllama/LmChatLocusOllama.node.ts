import { supplyModel } from '@n8n/ai-node-sdk';
import type { INodeType, INodeTypeDescription, ISupplyDataFunctions, SupplyData } from 'n8n-workflow';
import { NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';

import { LocusFetchChatModel } from '../../src/locus-chat-model';
import { nodeShapes } from '../../src/node-shapes';
import { resolveServiceBaseUrl } from '../../src/openai-chat';

const shape = nodeShapes().ollama;

export class LmChatLocusOllama implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Locus Ollama Chat Model',
    name: 'lmChatLocusOllama',
    icon: 'fa:robot',
    group: ['transform'],
    version: [1],
    description: 'Chat model for the Locus Ollama server. No API key is sent.',
    defaults: { name: 'Locus Ollama Chat Model' },
    inputs: [],
    outputs: shape.aiLanguageModel
      ? [NodeConnectionTypes.AiLanguageModel]
      : [NodeConnectionTypes.Main],
    credentials: [{ name: shape.credential, required: true }],
    properties: [
      {
        displayName: 'Model',
        name: 'model',
        type: 'string',
        default: '',
        required: true,
        description: 'Name of a model already pulled on Locus Ollama. There is no baked-in model.',
      },
      {
        displayName: 'Temperature',
        name: 'temperature',
        type: 'number',
        default: 0.7,
        typeOptions: { minValue: 0, maxValue: 2, numberPrecision: 2 },
      },
    ],
  };

  async supplyData(this: ISupplyDataFunctions, itemIndex: number): Promise<SupplyData> {
    const model = this.getNodeParameter('model', itemIndex) as string;
    if (!model.trim()) {
      throw new NodeOperationError(this.getNode(), 'Locus Ollama model is empty');
    }
    const credentials = await this.getCredentials('locusOllamaApi');
    const baseUrl = resolveServiceBaseUrl(
      'ollama',
      String(credentials.baseUrl ?? ''),
      process.env,
    );
    const temperature = this.getNodeParameter('temperature', itemIndex) as number;
    const chatModel = new LocusFetchChatModel('ollama', model, baseUrl, undefined, temperature);
    return supplyModel(this, chatModel);
  }
}
