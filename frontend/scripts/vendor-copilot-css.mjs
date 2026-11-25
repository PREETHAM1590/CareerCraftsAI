import { readFileSync, writeFileSync } from "node:fs";
import postcss from "postcss";

const src = "node_modules/@copilotkit/react-core/dist/v2/index.css";
const dst = "src/app/(app)/nova/copilot-v2.css";

const root = postcss.parse(readFileSync(src, "utf-8"));

// Robust approach: recursively unwrap all @layer atrules anywhere.
function unwrap(rootNode) {
  let found = true;
  while (found) {
    found = false;
    const stack = [rootNode];
    while (stack.length) {
      const n = stack.pop();
      for (const c of [...(n.nodes ?? [])]) {
        if (c.type === "atrule" && c.name === "layer") {
          const parent = c.parent;
          const idx = parent.index(c);
          for (const g of [...(c.nodes ?? [])]) {
            parent.insertAfter(parent.nodes[idx], g);
          }
          c.remove();
          found = true;
        } else {
          stack.push(c);
        }
      }
    }
  }
}

unwrap(root);
const header = `/* Vendored from @copilotkit/react-core/dist/v2/index.css with @layer wrappers
   stripped (Tailwind's PostCSS plugin rejects foreign @layer usage).
   Regenerate: bump @copilotkit/react-core, then re-run scripts/vendor-copilot-css.mjs. */\n`;
writeFileSync(dst, header + root.toString() + "\n");
console.log("wrote", dst, root.toString().length, "chars");
