const { createRequire } = require('module');
const fs = require('fs');
const path = require('path');

function packageRoot(resolved, expectedName) {
  let dir = path.dirname(resolved);
  while (dir !== path.dirname(dir)) {
    const pkgPath = path.join(dir, 'package.json');
    if (fs.existsSync(pkgPath)) {
      const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
      if (pkg.name === expectedName) return dir;
    }
    dir = path.dirname(dir);
  }
  throw new Error(`package root for ${expectedName} not found from ${resolved}`);
}

const imageRequire = createRequire('/usr/local/lib/node_modules/n8n/package.json');
const destRoot = '/opt/locus-n8n-nodes/node_modules';
fs.mkdirSync(path.join(destRoot, '@n8n'), { recursive: true });

const packages = [
  { name: '@n8n/ai-node-sdk', dest: path.join(destRoot, '@n8n', 'ai-node-sdk') },
  { name: 'n8n-workflow', dest: path.join(destRoot, 'n8n-workflow') },
];

for (const pkg of packages) {
  const resolved = imageRequire.resolve(pkg.name);
  const root = packageRoot(resolved, pkg.name);
  if (root.startsWith('/opt/locus-n8n-nodes')) {
    throw new Error(`${pkg.name} resolved inside the custom package (${root})`);
  }
  fs.symlinkSync(root, pkg.dest);
}

const nodeRequire = createRequire('/opt/locus-n8n-nodes/dist/src/openai-chat.js');
for (const pkg of packages) {
  const fromNode = packageRoot(nodeRequire.resolve(pkg.name), pkg.name);
  const fromImage = packageRoot(imageRequire.resolve(pkg.name), pkg.name);
  if (fromNode !== fromImage) {
    throw new Error(`${pkg.name} copies differ: node=${fromNode} image=${fromImage}`);
  }
}

const nodeFiles = [
  '/opt/locus-n8n-nodes/dist/nodes/LocusHermes/LocusHermes.node.js',
  '/opt/locus-n8n-nodes/dist/nodes/LmChatLocusOllama/LmChatLocusOllama.node.js',
  '/opt/locus-n8n-nodes/dist/nodes/LmChatLocusHuggingFace/LmChatLocusHuggingFace.node.js',
  '/opt/locus-n8n-nodes/dist/credentials/LocusAuthentikOAuth2Api.credentials.js',
  '/opt/locus-n8n-nodes/dist/credentials/LocusHermesApi.credentials.js',
  '/opt/locus-n8n-nodes/dist/credentials/LocusOllamaApi.credentials.js',
  '/opt/locus-n8n-nodes/dist/credentials/LocusHuggingFaceApi.credentials.js',
];

for (const file of nodeFiles) {
  const loaded = require(file);
  const className = path.basename(file).split('.')[0];
  const Type = loaded[className];
  if (typeof Type !== 'function') {
    throw new Error(`${file} does not export ${className}`);
  }
  const instance = new Type();
  if (!instance.description && !instance.properties) {
    throw new Error(`${className} constructed without a description or properties`);
  }
}
