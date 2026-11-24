import { useEffect, useState } from "react";
import { toaster } from "@decky/api";
import * as backend from "./backend";
import type { Job, Kind, Meta } from "./kinds";

export interface Status {
  state: string;   // idle | connecting | connected | disconnected | error | pairing
  message?: string;
  kind?: string;
  device?: string | null;
  battery?: number | null;
}
export interface Pin { key: string; path: string; kind: Kind; label?: string | null; signal?: string; meta?: Meta }
export interface Settings { folders: Record<string, string> }   // the last folder browsed, under "all"
export interface Owner { live: boolean; blocked: null | "window" | "busy" }
export interface FileEntry { name: string; size: number; path: string; kind: Kind; meta?: Meta }
export interface Listing {
  path: string;
  all?: boolean;
  kind: Kind | null;
  dirs: string[] | null;
  files: FileEntry[] | null;
  remote?: boolean;
  signals?: { name: string; protocol?: string }[];
  offline?: boolean;
}
export interface Snapshot {
  status: Status;
  favorites: Pin[];
  settings: Settings;
  owner: Owner;
  version?: string;
  plugin_version?: string;
}

export interface FoundFlipper { address: string; name: string; paired: boolean; nearby: boolean }
export interface Pairing {
  open: boolean;
  step: string;      // starting, searching, testing, works, confirm, removing, pairing, code, checking, done, error
  device: string;
  devices: FoundFlipper[];
  message: string;
  code: string;      // what's been typed on the number pad
}
const PAIRING_CLOSED: Pairing = { open: false, step: "", device: "your Flipper", devices: [], message: "", code: "" };

export interface State {
  ready: boolean;
  status: Status;
  favorites: Pin[];
  settings: Settings;
  owner: Owner;
  listings: Record<string, Listing>;
  listError: Record<string, string>;
  meta: Record<string, Meta>;
  jobs: Record<string, Job>;   // key -> the latest press of that file (or remote button)
  holding: Set<string>;        // keys being held down right now
  problem: string | null;      // the panel couldn't reach its backend
  pairing: Pairing;            // "Pair a Flipper": kept here, so it's still there when the panel is opened again
  version: string;
  pluginVersion: string;
}

let state: State = {
  ready: false,
  status: { state: "idle" },
  favorites: [],
  settings: { folders: {} },
  owner: { live: false, blocked: null },
  listings: {},
  listError: {},
  meta: {},
  jobs: {},
  holding: new Set(),
  problem: null,
  pairing: PAIRING_CLOSED,
  version: "",
  pluginVersion: "",
};
const listeners = new Set<() => void>();

export const getState = () => state;

export function update(change: (s: State) => void) {
  const next = { ...state };
  change(next);
  state = next;
  listeners.forEach((fn) => fn());
}

export function useStore(): State {
  const [, setTick] = useState(0);
  useEffect(() => {
    const fn = () => setTick((n) => n + 1);
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  }, []);
  return state;
}

export function notify(message: string) {
  try {
    toaster.toast({ title: "Flipper Remote", body: message });
  } catch {
    // no toasts available: the message still shows on the button
  }
}

export function problem(e: unknown) {
  update((s) => {
    s.problem = backend.messageOf(e);
  });
}

export function applySnapshot(snap: Snapshot) {
  update((s) => {
    s.ready = true;
    s.problem = null;
    if (snap.status) s.status = snap.status;
    s.favorites = snap.favorites || [];
    if (snap.settings) s.settings = foldersOf(snap.settings);
    if (snap.owner) s.owner = snap.owner;
    s.version = snap.version || s.version;
    s.pluginVersion = snap.plugin_version || s.pluginVersion;
    const meta = { ...s.meta };
    for (const f of s.favorites) if (f.meta && !f.signal) meta[f.path] = f.meta;
    s.meta = meta;
  });
}

// -- presses ------------------------------------------------------------------------------------------------

const doneTimers: Record<string, ReturnType<typeof setTimeout>> = {};

export function onJob(ev: Job, key = ev.key || ev.path || "") {
  if (!key) return;
  const prev = state.jobs[key];
  if (prev && prev.id && ev.id && ev.id < prev.id) return;   // a stale update from an older press
  if (ev.phase === "emulating") ev.until = Date.now() + (ev.seconds || 30) * 1000;
  update((s) => {
    s.jobs = { ...s.jobs, [key]: ev };
  });
  clearTimeout(doneTimers[key]);
  if (ev.phase === "done" || ev.phase === "error") {
    doneTimers[key] = setTimeout(() => {
      if (state.jobs[key] === ev) {
        update((s) => {
          const jobs = { ...s.jobs };
          delete jobs[key];
          s.jobs = jobs;
        });
      }
    }, ev.phase === "done" ? 1600 : 5000);
    if (ev.phase === "error" && ev.message) notify(ev.message);
  }
}

export function setHolding(key: string, on: boolean) {
  update((s) => {
    const holding = new Set(s.holding);
    if (on) holding.add(key);
    else holding.delete(key);
    s.holding = holding;
  });
}

// -- the backend's live updates ("flipper" events) ---------------------------------------------------------------

export function onEvent(event: string, data: any) {
  switch (event) {
    case "hello":
      applySnapshot(data as Snapshot);
      break;
    case "status":
      update((s) => {
        s.status = data as Status;
      });
      break;
    case "owner":
      update((s) => {
        s.owner = data as Owner;
      });
      break;
    case "files":
      update((s) => {
        s.listings = { ...s.listings, [data.path]: data as Listing };
        const errors = { ...s.listError };
        delete errors[data.path];
        s.listError = errors;
        const meta = { ...s.meta };
        for (const f of (data as Listing).files || []) if (f.meta) meta[f.path] = f.meta;
        s.meta = meta;
      });
      break;
    case "files_error":
      update((s) => {
        if (!s.listings[data.path]) s.listError = { ...s.listError, [data.path]: String(data.message) };
      });
      break;
    case "meta":
      update((s) => {
        s.meta = { ...s.meta, [data.path]: data.meta as Meta };
      });
      break;
    case "job":
      onJob(data as Job);
      break;
    case "favorites":
      update((s) => {
        s.favorites = data as Pin[];
      });
      break;
    case "settings":
      update((s) => {
        s.settings = foldersOf(data as Partial<Settings>);
      });
      break;
    case "pairing":
      onPairingStep(data as Partial<Pairing> & { step: string });
      break;
    case "helper_exit":
      update((s) => {
        s.status = { state: "idle" };
        s.owner = { live: false, blocked: null };
        s.problem = "The plugin's helper stopped. It starts again when you press something.";
        if (s.pairing.open) s.pairing = { ...s.pairing, step: "error", message: "The plugin's helper stopped. Try again." };
      });
      break;
  }
}

// -- being open ---------------------------------------------------------------------------------------------
// The backend holds on to the Flipper only while the panel or the Browse page is open (and for a few seconds
// after), so the Flipper Remote window can always get it back. Open views say so every couple of seconds.

const BEAT_MS = 2500;
let openViews = 0;
let beatTimer: ReturnType<typeof setInterval> | null = null;

async function beat() {
  try {
    const owner = await backend.heartbeat();
    update((s) => {
      s.owner = owner;
      s.problem = null;
    });
  } catch (e) {
    problem(e);
  }
}

async function refresh() {
  try {
    applySnapshot(await backend.hello());
  } catch (e) {
    problem(e);
  }
}

function viewOpened() {
  openViews += 1;
  if (openViews > 1) return;
  void refresh().then(beat);
  beatTimer = setInterval(() => void beat(), BEAT_MS);
}

function viewClosed() {
  openViews -= 1;
  if (openViews > 0) return;
  if (beatTimer) clearInterval(beatTimer);
  beatTimer = null;
  void backend.closed().catch(() => undefined);
}

export function usePresence(open: boolean) {
  useEffect(() => {
    if (!open) return undefined;
    viewOpened();
    return viewClosed;
  }, [open]);
}

// -- the last folder browsed (the window's memory for "All"; the plugin always shows everything) ------------------

// Only the folder memory: the window's Show choice and its other settings are the window's own.
function foldersOf(settings: Partial<Settings> | undefined): Settings {
  const folders = settings && typeof settings.folders === "object" && settings.folders ? settings.folders : {};
  return { folders: { ...folders } };
}

export function rememberFolder(path: string) {
  if (state.settings.folders.all === path) return;
  update((s) => {
    s.settings = { ...s.settings, folders: { ...s.settings.folders, all: path } };
  });
  backend.setSettings({ folders: { all: path } }, "browse").catch(() => undefined);
}

// -- "Pair a Flipper" (the app's own pairing, with the window's steps) -------------------------------------------

const PAIRING_ENDED = ["done", "works", "error", "cancelled"];

// Pairing is under way (not just showing how it ended): presses and Connect wait until it's over.
export function pairingInProgress(p: Pairing): boolean {
  return p.open && !PAIRING_ENDED.includes(p.step);
}

function setPairing(change: Partial<Pairing>) {
  update((s) => {
    s.pairing = { ...s.pairing, ...change };
  });
}

function pairingFailed(e: unknown) {
  setPairing({ step: "error", message: backend.messageOf(e) });
}

export function onPairingStep(ev: Partial<Pairing> & { step: string }) {
  if (!state.pairing.open) return;
  if (ev.step === "cancelled") {
    update((s) => {
      s.pairing = PAIRING_CLOSED;
    });
    return;
  }
  const change: Partial<Pairing> = { step: ev.step, message: ev.message || "" };
  if (ev.device) change.device = ev.device;
  if (ev.devices) change.devices = ev.devices;
  if (ev.step === "code") change.code = "";
  setPairing(change);
}

export function startPairing(via: string) {
  update((s) => {
    s.pairing = { ...PAIRING_CLOSED, open: true, step: "starting" };
  });
  backend.pair("start", null, null, null, via).catch(pairingFailed);
}

export function closePairing(cancel: boolean, via: string) {
  if (cancel && state.pairing.open && !PAIRING_ENDED.includes(state.pairing.step)) {
    backend.pair("cancel", null, null, null, via).catch(() => undefined);
  }
  update((s) => {
    s.pairing = PAIRING_CLOSED;
  });
}

export function chooseFlipper(d: FoundFlipper, via: string) {
  setPairing({ step: d.paired ? "testing" : "pairing", device: d.name });
  backend.pair("choose", d.address, null, null, via).catch(pairingFailed);
}

export function confirmPairing(remove: boolean, via: string) {
  if (remove) setPairing({ step: "removing" });
  backend.pair("confirm", null, remove, null, via).catch(pairingFailed);
  if (!remove) closePairing(false, via);
}

export function codeKey(key: string) {
  const p = state.pairing;
  if (p.step !== "code") return;
  if (key === "del") setPairing({ code: p.code.slice(0, -1) });
  else if (key === "ok") {
    if (p.code.length !== 6) return;
    setPairing({ step: "checking" });
    backend.pair("code", null, null, p.code, "").catch(pairingFailed);
  } else if (/^\d$/.test(key) && p.code.length < 6) setPairing({ code: p.code + key });
}
