import type { ICredentialType, INodeProperties } from 'n8n-workflow';

export class LocusHuggingFaceApi implements ICredentialType {
  name = 'locusHuggingFaceApi';

  displayName = 'Locus Hugging Face';

  properties: INodeProperties[] = [
    {
      displayName: 'API Token',
      name: 'apiKey',
      type: 'string',
      typeOptions: { password: true },
      default: '',
      required: true,
      description: 'Hugging Face token. Stored in this credential, not in the image.',
    },
    {
      displayName: 'Model',
      name: 'model',
      type: 'string',
      default: '',
      description: 'Optional. When set, overrides the model on the chat node. Include a provider suffix such as :cheapest.',
    },
  ];
}
