import type { ICredentialType, INodeProperties } from 'n8n-workflow';

import { authentikCredentialShape } from '../src/credential-shapes';

const shape = authentikCredentialShape();

export class LocusAuthentikOAuth2Api implements ICredentialType {
  name = 'locusAuthentikOAuth2Api';

  extends = ['oAuth2Api'];

  displayName = 'Locus Authentik (client credentials)';

  properties: INodeProperties[] = [
    {
      displayName: 'Grant Type',
      name: 'grantType',
      type: 'hidden',
      default: shape.grantType,
    },
    {
      displayName: 'Access Token URL',
      name: 'accessTokenUrl',
      type: 'string',
      default: shape.accessTokenUrlDefault,
      required: true,
      placeholder: 'https://<authentik-public-host>/application/o/token/',
      description: 'Authentik token endpoint. Client credentials only; no browser redirect.',
    },
    {
      displayName: 'Client ID',
      name: 'clientId',
      type: 'string',
      default: '',
      required: true,
    },
    {
      displayName: 'Client Secret',
      name: 'clientSecret',
      type: 'string',
      typeOptions: { password: shape.clientSecretPassword },
      default: '',
      required: true,
    },
    {
      displayName: 'Client Authentication',
      name: 'authentication',
      type: 'hidden',
      default: shape.authentication,
    },
    {
      displayName: 'Scope',
      name: 'scope',
      type: 'string',
      default: '',
    },
  ];
}
