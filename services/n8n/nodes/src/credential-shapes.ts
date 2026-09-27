export interface AuthentikCredentialShape {
  grantType: string;
  authentication: string;
  accessTokenUrlDefault: string;
  hasAuthUrl: boolean;
  clientSecretPassword: boolean;
}

export function authentikCredentialShape(): AuthentikCredentialShape {
  return {
    grantType: 'clientCredentials',
    authentication: 'body',
    accessTokenUrlDefault: '',
    hasAuthUrl: false,
    clientSecretPassword: true,
  };
}
