import type { KeyboardEvent } from "react";

/** True while an input method (Japanese, Chinese, Korean, Hindi…) is still
 * composing, so Enter confirms a character instead of submitting.
 *
 * Browsers differ: Chrome and Firefox report `isComposing` on that keydown,
 * but Safari fires compositionend first and reports `isComposing: false`
 * with the legacy `keyCode` 229 (a long-standing WebKit behaviour). Both checks
 * are needed; don't drop the deprecated `keyCode` one. */
export function isComposing(event: KeyboardEvent): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}
