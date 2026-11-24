import { CSSProperties, useEffect, useRef, useState } from "react";
import { DialogButton, GamepadEvent } from "@decky/ui";
import { FaRegStar, FaStar } from "react-icons/fa";
import * as backend from "./backend";
import { baseName, Job, Kind, KINDS, Meta, metaText, phaseText, pretty, running } from "./kinds";
import { getState, notify, onJob, setHolding, State, update, useStore } from "./state";
import { ACTIVE, ERROR, LINE1, LINE2, scrollIntoView } from "./styles";

// One thing to press: a saved Sub-GHz signal, a button of an Infrared remote, or an RFID key, NFC card or
// iButton key (those are emulated: tap to start, tap again to stop).
export interface Item {
  key: string;
  path: string;
  kind: Kind;
  label: string;
  signal?: string;      // the button, for an Infrared remote
  protocol?: string;    // an Infrared button's protocol, from the remote's listing
  meta?: Meta;
}

const HOLD_AFTER_MS = 450;   // like the window: held this long, it keeps sending until let go
const OK_BUTTON = 1;         // GamepadButton.OK (the A button)

export function infoText(item: Item, s: State): string {
  if (item.signal) {
    const sig = (s.meta[item.path]?.signals || []).find((x) => x.name === item.signal);
    return sig?.protocol || item.protocol || item.meta?.protocol || "";
  }
  return metaText(s.meta[item.path] || item.meta, item.kind);
}

function markJob(key: string, job: Job) {
  onJob(job, key);
}

function failed(key: string, e: unknown) {
  markJob(key, { phase: "error" });
  notify(backend.messageOf(e));
}

function sendNow(item: Item, via: string) {
  markJob(item.key, { phase: "queued" });
  backend.send(item.path, item.signal ?? null, via).catch((e) => failed(item.key, e));
}

function toggleEmulation(item: Item, via: string) {
  if (running(getState().jobs[item.key])) {
    backend.stop(item.path, null, via).catch((e) => notify(backend.messageOf(e)));
    return;
  }
  sendNow(item, via);
}

// Taps arrive as ordinary clicks (Steam's A button and the touchscreen both make one), so a tap always works.
// Holding is watched separately, from the screen's pointer down/up and the controller's button down/up:
//  - a click that comes while the button is still down waits for the release, so a press is a tap or a hold,
//    whichever order Steam delivers these in;
//  - once a hold has started, the click that comes with letting go is ignored;
//  - the controller can hold only once this panel has seen it report a release (so a Steam that never reports
//    one can't turn every press into a long hold), and a hold stops by itself after 30 s, like the app's.
const MAX_HOLD_MS = 30_000;
let controllerReportsRelease = false;

function usePress(item: Item) {
  const emulate = KINDS[item.kind].emulate;
  const latest = useRef(item);
  latest.current = item;
  const st = useRef({ down: false, holding: false, pendingTap: false, ignoreClickUntil: 0, via: "", viaAt: 0,
                      timer: undefined as ReturnType<typeof setTimeout> | undefined,
                      cap: undefined as ReturnType<typeof setTimeout> | undefined });

  const noteVia = (via: string) => {
    st.current.via = via;
    st.current.viaAt = Date.now();
  };
  const recentVia = () => (Date.now() - st.current.viaAt < 2000 ? st.current.via : "click");
  const tap = () => {
    if (emulate) toggleEmulation(latest.current, recentVia());
    else sendNow(latest.current, recentVia());
  };

  const end = () => {
    const s = st.current;
    if (!s.down) return;
    s.down = false;
    clearTimeout(s.timer);
    clearTimeout(s.cap);
    if (s.holding) {
      s.holding = false;
      s.pendingTap = false;
      s.ignoreClickUntil = Date.now() + 800;
      const it = latest.current;
      setHolding(it.key, false);
      backend.hold(it.path, it.signal ?? null, "stop", "").catch(() => undefined);
    } else if (s.pendingTap) {   // its click came on press: it was a tap after all
      s.pendingTap = false;
      tap();
    }
  };

  const start = (via: string) => {
    const s = st.current;
    if (emulate || s.down) return;
    s.down = true;
    s.holding = false;
    s.pendingTap = false;
    s.timer = setTimeout(() => {
      s.holding = true;
      s.pendingTap = false;   // a click that came on press is part of this hold
      s.ignoreClickUntil = Number.POSITIVE_INFINITY;
      const it = latest.current;
      setHolding(it.key, true);
      backend.hold(it.path, it.signal ?? null, "start", via).catch((e) => failed(it.key, e));
      s.cap = setTimeout(end, MAX_HOLD_MS);
    }, HOLD_AFTER_MS);
  };

  const click = () => {
    const s = st.current;
    if (s.holding) return;
    if (Date.now() < s.ignoreClickUntil) {   // the click that comes with letting go of a hold
      s.ignoreClickUntil = 0;
      return;
    }
    if (s.down) {   // still pressed: tap or hold is decided on release
      s.pendingTap = true;
      return;
    }
    tap();
  };

  useEffect(() => () => end(), []);   // closed while held: let go

  return {
    onClick: () => click(),
    onButtonDown: (e: GamepadEvent) => {
      if (e.detail.button !== OK_BUTTON || e.detail.is_repeat) return;
      noteVia("controller:A");
      if (controllerReportsRelease) start("controller:A");
    },
    onButtonUp: (e: GamepadEvent) => {
      if (e.detail.button !== OK_BUTTON) return;
      controllerReportsRelease = true;
      end();
    },
    onPointerDown: (e: PointerEvent) => {
      if (e.button !== 0) return;
      const via = e.pointerType === "touch" ? "touch" : e.pointerType || "mouse";
      noteVia(via);
      start(via);
    },
    onPointerUp: () => end(),
    onPointerCancel: () => end(),
    onGamepadBlur: () => end(),
  };
}

function useTicking(on: boolean) {   // the countdown on a card or key being emulated
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!on) return undefined;
    const timer = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [on]);
}

export function PressButton({ item, disabled, style }: { item: Item; disabled?: boolean; style?: CSSProperties }) {
  const s = useStore();
  const job = s.jobs[item.key];
  const emulate = KINDS[item.kind].emulate;
  useTicking(job?.phase === "emulating");
  const press = usePress(item);
  const info = (job && phaseText(item.kind, job, s.holding.has(item.key))) || infoText(item, s);
  const active = job && (job.phase === "sending" || job.phase === "emulating");
  const edge = job?.phase === "error" ? ERROR : active ? ACTIVE : "transparent";
  return (
    <DialogButton
      {...press}
      disabled={disabled}
      onGamepadFocus={scrollIntoView}
      onOKActionDescription={emulate ? (running(job) ? "Stop" : "Emulate") : "Send (hold to keep sending)"}
      style={{ textAlign: "left", padding: "8px 12px", minWidth: 0, borderLeft: `4px solid ${edge}`, ...style }}
    >
      <div style={LINE1}>{item.label}</div>
      <div style={LINE2}>{info || " "}</div>
    </DialogButton>
  );
}

export function PinButton({ item }: { item: Item }) {
  const s = useStore();
  const pinned = s.favorites.some((f) => f.key === item.key);
  const toggle = async () => {
    // a remote's button is pinned under the remote's name too ("TV Power"), so it makes sense on its own
    const label = item.signal ? `${pretty(baseName(item.path))} ${item.signal}` : item.label;
    try {
      const favorites = await backend.setFavorite(item.path, item.signal ?? null, !pinned, label, "browse");
      update((st) => {
        st.favorites = favorites;
      });
    } catch (e) {
      notify(backend.messageOf(e));
    }
  };
  return (
    <DialogButton
      onClick={() => void toggle()}
      onGamepadFocus={scrollIntoView}
      onOKActionDescription={pinned ? "Unpin" : "Pin"}
      style={{ width: "auto", minWidth: "7.5em", padding: "8px 12px", display: "flex", alignItems: "center",
               justifyContent: "center", gap: "6px" }}
    >
      {pinned ? <FaStar /> : <FaRegStar />}
      <span>{pinned ? "Pinned" : "Pin"}</span>
    </DialogButton>
  );
}
