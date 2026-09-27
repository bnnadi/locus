import type { ICredentialType, INodeProperties } from 'n8n-workflow';

export class LocusOllamaApi implements ICredentialType {
  name = 'locusOllamaApi';

  displayName = 'Locus Ollama';

  properties: INodeProperties[] = [
    {
      displayName: 'Base URL',
      name: 'baseUrl',
      type: 'string',
      default: '',
      description:
        'Leave empty to use OLLAMA_URL from the n8n container. Locus Ollama listens on port 8080. No API key is sent.',
    },
  ];
}
