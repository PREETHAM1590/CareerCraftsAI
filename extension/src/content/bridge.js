// CareerCraft AI — bridge between the web app and the extension.
// Runs on the hosted site at install time. Other app origins register it
// dynamically after pairing. Classic script.
(function () {
  if (window.__ccBridgeLoaded) return;
  window.__ccBridgeLoaded = true;

  function post(data) {
    window.postMessage(data, location.origin);
  }

  try {
    post({
      source: "careercraft-extension",
      type: "CAREERCRAFT_EXTENSION_READY",
      version: chrome.runtime.getManifest().version,
    });
  } catch (e) {
    /* extension context can be gone right after an update/reload */
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.origin !== location.origin) return;
    const data = event.data;
    if (!data || data.source !== "careercraft-app") return;

    if (data.type === "CAREERCRAFT_PING") {
      try {
        post({
          source: "careercraft-extension",
          type: "CAREERCRAFT_EXTENSION_READY",
          version: chrome.runtime.getManifest().version,
        });
      } catch (e) {
        /* extension context gone */
      }
      return;
    }

    if (data.type === "CAREERCRAFT_AGENT_WAKE") {
      try {
        chrome.runtime.sendMessage({ type: "AGENT_WAKE" });
      } catch (e) {
        /* ignore */
      }
      return;
    }

    if (data.type === "CAREERCRAFT_WAKE") {
      try {
        chrome.runtime.sendMessage({ type: "WAKE" });
      } catch (e) {
        /* ignore */
      }
      return;
    }

    if (data.type === "CAREERCRAFT_PAIR") {
      // Same shape the background re-checks; the page can only ever offer a
      // token for its own origin, and a different account still needs the
      // member to confirm in the extension popup.
      if (typeof data.token !== "string" || !/^ccx_[A-Za-z0-9_-]{8,512}$/.test(data.token)) return;
      if (typeof data.appOrigin !== "string" || data.appOrigin !== location.origin) return;
      try {
        chrome.runtime.sendMessage(
          { type: "CC_PAIR_FROM_BRIDGE", token: data.token, appOrigin: data.appOrigin },
          (response) => {
            post({
              source: "careercraft-extension",
              type: "CAREERCRAFT_PAIRED",
              ok: !!(response && response.ok),
              reason: response && response.reason,
            });
          }
        );
      } catch (e) {
        post({ source: "careercraft-extension", type: "CAREERCRAFT_PAIRED", ok: false, reason: "extension_error" });
      }
    }
  });
})();
