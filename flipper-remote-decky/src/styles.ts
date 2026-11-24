import type { CSSProperties } from "react";

export const ACTIVE = "#ff8f1f";   // the Flipper Remote orange: sending or emulating
export const ERROR = "#e5534b";

export const NOTE: CSSProperties = { fontSize: "12px", lineHeight: "16px", opacity: 0.75, whiteSpace: "normal" };
export const LINE1: CSSProperties = { fontWeight: 600, fontSize: "14px", lineHeight: "18px", overflow: "hidden",
                                      textOverflow: "ellipsis", whiteSpace: "nowrap" };
export const LINE2: CSSProperties = { fontSize: "12px", lineHeight: "16px", opacity: 0.75, overflow: "hidden",
                                      textOverflow: "ellipsis", whiteSpace: "nowrap" };

// Steam moves focus with the controller; keep the focused row on screen in the Browse page's own scroll area.
export function scrollIntoView(e: Event) {
  const el = e.target as HTMLElement | null;
  el?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
}
