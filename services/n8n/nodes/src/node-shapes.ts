export interface NodeShapeDescriptor {
  aiLanguageModel: boolean;
  credential: string;
}

export interface NodeShapes {
  hermes: NodeShapeDescriptor;
  ollama: NodeShapeDescriptor;
  huggingface: NodeShapeDescriptor;
}

export function nodeShapes(): NodeShapes {
  return {
    hermes: { aiLanguageModel: false, credential: 'locusHermesApi' },
    ollama: { aiLanguageModel: true, credential: 'locusOllamaApi' },
    huggingface: { aiLanguageModel: true, credential: 'locusHuggingFaceApi' },
  };
}
