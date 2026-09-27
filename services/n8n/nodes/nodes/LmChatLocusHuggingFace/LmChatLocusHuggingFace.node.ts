import { supplyModel } from '@n8n/ai-node-sdk';
import type { INodeType, INodeTypeDescription, ISupplyDataFunctions, SupplyData } from 'n8n-workflow';
import { NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';

import { defaultHuggingFaceModel } from '../../src/defaults';
import { LocusFetchChatModel } from '../../src/locus-chat-model';
import { nodeShapes } from '../../src/node-shapes';
import { resolveServiceBaseUrl } from '../../src/openai-chat';

const shape = nodeShapes().huggingface;

export class LmChatLocusHuggingFace implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Locus Hugging Face Chat Model',
    name: 'lmChatLocusHuggingFace',
    icon: 'fa:robot',
    group: ['transform'],
    version: [1],
    description: 'Chat model for the Hugging Face Inference Providers router.',
    defaults: { name: 'Locus Hugging Face Chat Model' },
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
        default: defaultHuggingFaceModel(),
        description: 'Inference Providers model id. Keep a routing suffix such as :cheapest when the provider should be chosen for you.',
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
    const credentials = await this.getCredentials('locusHuggingFaceApi');
    const apiKey = String(credentials.apiKey ?? '');
    if (!apiKey) {
      throw new NodeOperationError(this.getNode(), 'Locus Hugging Face API token is empty');
    }
    const credentialModel = String(credentials.model ?? '').trim();
    const nodeModel = this.getNodeParameter('model', itemIndex) as string;
    const model = credentialModel || nodeModel;
    const baseUrl = resolveServiceBaseUrl('huggingface', '', process.env);
    const temperature = this.getNodeParameter('temperature', itemIndex) as number;
    const chatModel = new LocusFetchChatModel('huggingface', model, baseUrl, apiKey, temperature);
    return supplyModel(this, chatModel);
  }
}
