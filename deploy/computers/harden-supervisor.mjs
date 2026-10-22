import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

// Fail closed if the pinned upstream contract changes. Child authentication and
// a per-computer CPU cap change; OpenBot retains ownership of container
// creation, volumes and lifecycle.
export function hardenSupervisorEnvironment(source) {
  const before =
    'const computerToken = env.COMPUTER_TOKEN?.trim() || undefined;';
  if (source.split(before).length !== 2)
    throw new Error(
      'Pinned OpenBot environment contract changed; review before building.',
    );
  return `import { createHmac } from "node:crypto";\n${source.replace(
    before,
    `const master = env.COMPUTER_TOKEN?.trim();
  if (!master || master.length < 24) throw new Error("COMPUTER_TOKEN must contain at least 24 characters.");
  const computerToken = createHmac("sha256", master).update("opendots-computer:" + botId).digest("hex");`,
  )}`;
}

// One browser must not be able to take every core from the others: Docker's
// NanoCpus from COMPUTER_CPUS (cores, e.g. 1.5). Unset leaves it uncapped.
export function hardenSupervisorDocker(source) {
  const before = '    PidsLimit: options.pidsLimit ?? 512,';
  if (source.split(before).length !== 2)
    throw new Error(
      'Pinned OpenBot docker contract changed; review before building.',
    );
  return `${source.replace(before, `${before}
    ...computerCpuLimit(),`)}
function computerCpuLimit() {
  const raw = process.env.COMPUTER_CPUS?.trim();
  if (!raw) return {};
  const cpus = Number(raw);
  // 0.01 is Docker's minimum; anything that rounds to NanoCpus 0 would mean no cap.
  if (!(cpus >= 0.01 && cpus <= 64))
    throw new Error(\`COMPUTER_CPUS must be a number of cores from 0.01 to 64 (got \${JSON.stringify(raw)}).\`);
  return { NanoCpus: Math.round(cpus * 1e9) };
}
`;
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  const src = process.argv[2];
  if (!src) throw new Error('Pass the pinned supervisor src directory.');
  for (const [file, harden] of [
    ['environment.ts', hardenSupervisorEnvironment],
    ['docker.ts', hardenSupervisorDocker],
  ]) {
    const path = join(src, file);
    await writeFile(path, harden(await readFile(path, 'utf8')));
  }
}
