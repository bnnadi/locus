import { cpSync, mkdirSync, readdirSync } from 'node:fs';
import path from 'node:path';

const nodesDir = 'nodes';
for (const dir of readdirSync(nodesDir)) {
  const src = path.join(nodesDir, dir);
  const dest = path.join('dist', 'nodes', dir);
  mkdirSync(dest, { recursive: true });
  for (const file of readdirSync(src)) {
    if (file.endsWith('.node.json')) {
      cpSync(path.join(src, file), path.join(dest, file));
    }
  }
}
