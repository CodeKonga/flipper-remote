// What the Flipper keeps where, and how Flipper Remote words things. Mirrors the Flipper Remote window (ui.html),
// so the panel says the same things the window does.

export type Kind = "subghz" | "infrared" | "lfrfid" | "nfc" | "ibutton";
export type Show = "all" | Kind;

export interface KindInfo {
  root: string;
  ext: string;
  title: string;
  emulate: boolean;   // RFID, NFC and iButton are emulated (tap to start, tap again to stop), not sent
  empty: string;
}

export const KINDS: Record<Kind, KindInfo> = {
  subghz: { root: "/ext/subghz", ext: ".sub", title: "Sub-GHz", emulate: false,
            empty: "No saved signals in this folder. Signals you save in the Flipper's Sub-GHz app show up here." },
  infrared: { root: "/ext/infrared", ext: ".ir", title: "Infrared", emulate: false,
              empty: "No saved remotes in this folder. Remotes you save in the Flipper's Infrared app show up here." },
  lfrfid: { root: "/ext/lfrfid", ext: ".rfid", title: "RFID 125 kHz", emulate: true,
            empty: "No saved keys in this folder. Keys you save in the Flipper's 125 kHz RFID app show up here." },
  nfc: { root: "/ext/nfc", ext: ".nfc", title: "NFC", emulate: true,
         empty: "No saved cards in this folder. Cards you save in the Flipper's NFC app show up here." },
  ibutton: { root: "/ext/ibutton", ext: ".ibtn", title: "iButton", emulate: true,
             empty: "No saved keys in this folder. Keys you save in the Flipper's iButton app show up here." },
};
export const KIND_LIST = Object.keys(KINDS) as Kind[];
export const ALL_ROOT = "/ext";

export function kindOf(path: string): Kind | null {
  for (const kind of KIND_LIST) {
    const root = KINDS[kind].root;
    if (path === root || path.startsWith(root + "/")) return kind;
  }
  return null;
}

export function fileKind(path: string): Kind | null {
  const kind = kindOf(path);
  return kind && path !== KINDS[kind].root && path.toLowerCase().endsWith(KINDS[kind].ext) ? kind : null;
}

export const keyOf = (path: string, signal?: string | null) => (signal ? `${path}#${signal}` : path);
export const baseName = (path: string) => path.split("/").pop() || path;
export const pretty = (name: string) => name.replace(/\.(sub|ir|rfid|nfc|ibtn)$/i, "").replace(/_/g, " ");
export const parentOf = (path: string) => path.split("/").slice(0, -1).join("/") || ALL_ROOT;

// A folder the Browse page can open: "All" itself, or anything inside one of the kinds' folders.
export function inBrowse(path: unknown): path is string {
  if (typeof path !== "string" || path.split("/").includes("..")) return false;
  return path === ALL_ROOT || kindOf(path) !== null;
}

export interface Meta {
  frequency?: number;
  protocol?: string;
  raw_ms?: number;
  data?: string;
  buttons?: number;
  signals?: { name: string; protocol?: string }[];
}

export function metaText(m: Meta | undefined | null, kind: Kind | null): string {
  if (!m) return "";
  if (kind === "infrared") {
    const n = m.buttons != null ? m.buttons : (m.signals || []).length;
    return `${n} button${n === 1 ? "" : "s"}`;
  }
  if (kind && kind !== "subghz") return [m.protocol, m.data].filter(Boolean).join(", ");
  const parts: string[] = [];
  if (m.frequency) {
    const mhz = m.frequency / 1e6;
    parts.push(`${Number.isInteger(mhz) ? mhz : mhz.toFixed(3).replace(/0+$/, "")} MHz`);
  }
  if (m.protocol === "RAW" && m.raw_ms) parts.push(`RAW ${(m.raw_ms / 1000).toFixed(1)} s`);
  else if (m.protocol) parts.push(m.protocol);
  return parts.join(", ");
}

export interface Job {
  id?: number;
  key?: string;
  path?: string;
  phase: string;
  message?: string;
  hold?: boolean;
  emulate?: boolean;
  seconds?: number;
  until?: number;   // when an emulation ends (this side's clock)
  bytes?: number;
  size?: number;
}

export const running = (job?: Job) => !!job && !["done", "error"].includes(job.phase);

export function phaseText(kind: Kind, job: Job, holding: boolean): string {
  const k = KINDS[kind];
  switch (job.phase) {
    case "queued": return "Waiting…";
    case "connecting": return "Connecting…";
    case "reading": return job.size ? `Reading file… ${Math.min(100, Math.round((100 * (job.bytes || 0)) / job.size))}%`
                                    : "Reading file…";
    case "opening": return `Opening ${k.title}…`;
    case "loading": return "Loading…";
    case "sending": return job.hold || holding ? "Sending, let go to stop" : "Sending…";
    case "emulating": {
      const left = Math.max(0, Math.ceil(((job.until || 0) - Date.now()) / 1000));
      return `Emulating, ${left} s. Tap to stop`;
    }
    case "done": return k.emulate ? "Stopped" : "Sent";
    case "error": return k.emulate ? "Not emulated" : "Not sent";
  }
  return "";
}

export function hintText(show: Show): string {
  const k = show === "all" ? null : KINDS[show];
  return !k ? "Tap to send or emulate. Hold Sub-GHz and Infrared to keep sending."
    : k.emulate ? "Tap to emulate. Tap again to stop." : "Tap to send. Hold to keep sending.";
}

export const BLOCKED_TEXT: Record<string, string> = {
  window: "The Flipper Remote window is open, so it has the Flipper. Close it to use the panel.",
  busy: "Flipper Remote is busy with the Flipper (closing, or another command). Try again in a moment.",
};
