import type { KeyboardEvent } from "react";

/** True while an input method (Japanese, Chinese, Korean, Hindi…) is still
 * composing. Safari fires the confirming keydown with isComposing false but
 * keyCode 229, so both are checked. */
export function isComposing(event: KeyboardEvent): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}
