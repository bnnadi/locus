import type { ICredentialType, INodeProperties } from 'n8n-workflow';

export class LocusHermesApi implements ICredentialType {
  name = 'locusHermesApi';

  displayName = 'Locus Hermes API';

  properties: INodeProperties[] = [
    {
      displayName: 'Base URL',
      name: 'baseUrl',
      type: 'string',
      default: '',
      description:
        'Leave empty to use HERMES_URL from the n8n container. Set this only to override that host. Hermes does not accept Authentik tokens.',
    },
    {
      displayName: 'API Key',
      name: 'apiKey',
      type: 'string',
      typeOptions: { password: true },
      default: '',
      required: true,
      description: 'Hermes API_SERVER_KEY.',
    },
  ];
}
