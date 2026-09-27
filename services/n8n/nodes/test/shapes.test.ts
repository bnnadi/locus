import test from 'node:test';
import assert from 'node:assert/strict';

import { authentikCredentialShape } from '../src/credential-shapes.ts';
import { nodeShapes } from '../src/node-shapes.ts';

// -----------------------------------------------------------------------
// authentikCredentialShape
// -----------------------------------------------------------------------

test('authentikCredentialShape: uses the client-credentials grant type', () => {
  assert.equal(authentikCredentialShape().grantType, 'clientCredentials');
});

test('authentikCredentialShape: sends client credentials in the request body', () => {
  assert.equal(authentikCredentialShape().authentication, 'body');
});

test('authentikCredentialShape: has no baked-in access token URL default', () => {
  assert.equal(authentikCredentialShape().accessTokenUrlDefault, '');
});

test('authentikCredentialShape: has no baked-in auth URL', () => {
  assert.equal(authentikCredentialShape().hasAuthUrl, false);
});

test('authentikCredentialShape: masks the client secret as a password field', () => {
  assert.equal(authentikCredentialShape().clientSecretPassword, true);
});

// -----------------------------------------------------------------------
// nodeShapes
// -----------------------------------------------------------------------

test('nodeShapes: hermes is a one-shot completion node, not an AI Agent language model', () => {
  assert.equal(nodeShapes().hermes.aiLanguageModel, false);
});

test('nodeShapes: hermes uses the locusHermesApi credential', () => {
  assert.equal(nodeShapes().hermes.credential, 'locusHermesApi');
});

test('nodeShapes: ollama is an AI Agent language model sub-node', () => {
  assert.equal(nodeShapes().ollama.aiLanguageModel, true);
});

test('nodeShapes: ollama uses the locusOllamaApi credential', () => {
  assert.equal(nodeShapes().ollama.credential, 'locusOllamaApi');
});

test('nodeShapes: huggingface is an AI Agent language model sub-node', () => {
  assert.equal(nodeShapes().huggingface.aiLanguageModel, true);
});

test('nodeShapes: huggingface uses the locusHuggingFaceApi credential', () => {
  assert.equal(nodeShapes().huggingface.credential, 'locusHuggingFaceApi');
});

test('nodeShapes: none of the three node credentials is the Authentik OAuth2 credential', () => {
  const shapes = nodeShapes();
  for (const [name, shape] of Object.entries(shapes)) {
    assert.notEqual(
      shape.credential,
      'locusAuthentikOAuth2Api',
      `${name} credential must not equal locusAuthentikOAuth2Api`
    );
  }
});
