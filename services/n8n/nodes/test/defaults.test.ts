import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { defaultHuggingFaceModel, defaultHermesModel } from '../src/defaults.ts';

test('defaultHuggingFaceModel: returns the pinned default HF model id', () => {
  assert.equal(defaultHuggingFaceModel(), 'meta-llama/Llama-3.2-3B-Instruct:cheapest');
});

test('defaultHermesModel: returns the pinned default Hermes model id', () => {
  assert.equal(defaultHermesModel(), 'hermes-agent');
});

// -----------------------------------------------------------------------
// Source-text guard: defaults.ts must never hard-code Ollama's default port
// or a loopback address. Those belong to env/credential resolution only,
// never to model defaults.
// -----------------------------------------------------------------------

const testDir = path.dirname(fileURLToPath(import.meta.url));
const defaultsSource = readFileSync(path.join(testDir, '..', 'src', 'defaults.ts'), 'utf8');

test('source guard: src/defaults.ts does not contain the literal 11434', () => {
  assert.ok(
    !defaultsSource.includes('11434'),
    'src/defaults.ts must not hard-code the Ollama default port 11434'
  );
});

test('source guard: src/defaults.ts does not contain the literal 127.0.0.1', () => {
  assert.ok(
    !defaultsSource.includes('127.0.0.1'),
    'src/defaults.ts must not hard-code a loopback address'
  );
});
