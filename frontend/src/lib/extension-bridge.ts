/**
 * Talks to the CareerCraft browser extension through window.postMessage.
 * The extension's bridge content script runs on this origin once the
 * extension is installed (on the hosted site and localhost; on other origins
 * after the first pairing from its popup).
 */

const APP = "careercraft-app";
const EXTENSION = "careercraft-extension";

type ExtensionMessage = { source?: string; type?: string; ok?: boolean; reason?: string; version?: string };

function waitForReply(type: string, timeoutMs: number): Promise<ExtensionMessage | null> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      window.removeEventListener("message", onMessage);
      resolve(null);
    }, timeoutMs);
    function onMessage(event: MessageEvent<ExtensionMessage>) {
      if (event.source !== window || event.origin !== window.location.origin) return;
      if (event.data?.source !== EXTENSION || event.data.type !== type) return;
      window.clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve(event.data);
    }
    window.addEventListener("message", onMessage);
  });
}

function post(message: Record<string, unknown>) {
  window.postMessage({ source: APP, ...message }, window.location.origin);
}

/** Resolves to the extension version, or null when it is not installed here. */
export async function detectExtension(timeoutMs = 800): Promise<string | null> {
  const reply = waitForReply("CAREERCRAFT_EXTENSION_READY", timeoutMs);
  post({ type: "CAREERCRAFT_PING" });
  const message = await reply;
  return message ? message.version ?? "unknown" : null;
}

/** Hands a fresh device token to the extension. Resolves to true once it is connected. */
export async function pairExtension(token: string, timeoutMs = 5000): Promise<boolean> {
  const reply = waitForReply("CAREERCRAFT_PAIRED", timeoutMs);
  post({ type: "CAREERCRAFT_PAIR", token, appOrigin: window.location.origin });
  const message = await reply;
  return Boolean(message?.ok);
}

/** Asks the extension to check for new applications right away. */
export function wakeExtension() {
  post({ type: "CAREERCRAFT_WAKE" });
}

/** Asks the extension to start its agent command loop right away. */
export function wakeAgent() {
  post({ type: "CAREERCRAFT_AGENT_WAKE" });
}
