// Copies the extension into a clean directory.
//
//   node scripts/build.mjs --out <dir> [--dev]
//
// manifest.json is the production manifest: it grants nothing on localhost
// or 127.0.0.1, so the package the backend serves never lets pages on a
// member's own machine reach the extension. --dev adds those hosts for local
// development and the test harnesses, which pair with an app on 127.0.0.1.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const DEV_HOSTS = ["http://localhost/*", "http://127.0.0.1/*"];
const EXCLUDED = new Set(["test", "scripts", "node_modules", "dist"]);

export function devManifest(manifest) {
  const next = structuredClone(manifest);
  next.host_permissions = [...DEV_HOSTS, ...next.host_permissions];
  for (const script of next.content_scripts || []) script.matches = [...DEV_HOSTS, ...script.matches];
  return next;
}

export function buildExtension({ out, dev = false }) {
  fs.rmSync(out, { recursive: true, force: true });
  fs.cpSync(root, out, {
    recursive: true,
    filter: (src) => {
      const rel = path.relative(root, src);
      return !rel.split(path.sep).some((part) => EXCLUDED.has(part) || part.startsWith("."));
    },
  });
  if (dev) {
    const file = path.join(out, "manifest.json");
    const manifest = JSON.parse(fs.readFileSync(file, "utf8"));
    fs.writeFileSync(file, JSON.stringify(devManifest(manifest), null, 2) + "\n");
  }
  return out;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const at = args.indexOf("--out");
  if (at < 0 || !args[at + 1]) {
    console.error("usage: node scripts/build.mjs --out <dir> [--dev]");
    process.exit(2);
  }
  console.log(buildExtension({ out: path.resolve(args[at + 1]), dev: args.includes("--dev") }));
}
