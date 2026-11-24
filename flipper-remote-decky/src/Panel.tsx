import { ButtonItem, Navigation, PanelSection, PanelSectionRow,
         useQuickAccessVisible as uiQuickAccessVisible } from "@decky/ui";
import { useQuickAccessVisible as apiQuickAccessVisible } from "@decky/api";
import * as backend from "./backend";
import { baseName, BLOCKED_TEXT, hintText, pretty } from "./kinds";
import { openPairing, viaOf } from "./Pairing";
import { Item, PressButton } from "./PressButton";
import { notify, pairingInProgress, Pin, State, usePresence, useStore } from "./state";
import { NOTE } from "./styles";

export const BROWSE_ROUTE = "/flipper-remote";

// Whether the Quick Access Menu is showing: from Decky's API, or its UI library on an older Decky, or failing
// both, "open while shown". (Decided once, so it's always the same hook.)
const useQuickAccessVisible: () => boolean =
  typeof apiQuickAccessVisible === "function" ? apiQuickAccessVisible
    : typeof uiQuickAccessVisible === "function" ? uiQuickAccessVisible : () => true;

export function pinItem(f: Pin): Item {
  return { key: f.key, path: f.path, kind: f.kind, signal: f.signal, meta: f.meta,
           label: pretty(f.label || baseName(f.path)) };
}

function statusLines(s: State): [string, string] {
  const st = s.status;
  if (s.problem) return ["Something went wrong", s.problem];
  if (s.owner.blocked) return [s.owner.blocked === "window" ? "In use by the window" : "Busy",
                               BLOCKED_TEXT[s.owner.blocked]];
  switch (st.state) {
    case "connected":
      return [st.device || "Flipper", typeof st.battery === "number" ? `Connected, battery ${st.battery}%` : "Connected"];
    case "connecting":
      return ["Connecting…", st.message || ""];
    case "pairing":
      return ["Pairing…", st.message || ""];
    case "error":
    case "disconnected":
      return ["Not connected", st.message || ""];
  }
  return [s.ready ? "Not connected" : "Starting…", ""];
}

export function StatusBlock() {
  const s = useStore();
  const [title, detail] = statusLines(s);
  return (
    <div style={{ padding: "2px 0 6px" }}>
      <div style={{ fontWeight: 600, fontSize: "15px" }}>{title}</div>
      {detail ? <div style={NOTE}>{detail}</div> : null}
    </div>
  );
}

export function ConnectionButton({ layout = "below" }: { layout?: "below" | "inline" }) {
  const s = useStore();
  const st = s.status.state;
  const connected = st === "connected";
  const busy = st === "connecting" || st === "pairing" || pairingInProgress(s.pairing);
  const label = connected ? "Disconnect" : st === "connecting" ? "Connecting…" : st === "error" ? "Try again" : "Connect";
  const onClick = () => {
    (connected ? backend.disconnect() : backend.connect()).catch((e) => notify(backend.messageOf(e)));
  };
  return (
    <ButtonItem layout={layout} disabled={busy || s.owner.blocked === "window"} onClick={onClick}>
      {label}
    </ButtonItem>
  );
}

function openBrowse() {
  Navigation.CloseSideMenus();
  Navigation.Navigate(BROWSE_ROUTE);
}

// The Quick Access Menu panel: how the Flipper is doing, every pinned file to press, Browse files, and
// "Pair a Flipper" at the bottom (it opens a dialog, like the window's).
export function Panel() {
  const visible = useQuickAccessVisible();
  usePresence(visible);
  const s = useStore();
  const pins = s.favorites;   // all of them: the plugin has no Show menu
  const busy = s.owner.blocked === "window" || pairingInProgress(s.pairing);
  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <StatusBlock />
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Pinned">
        {pins.map((f) => (
          <PanelSectionRow key={f.key}>
            <PressButton item={pinItem(f)} disabled={busy} style={{ width: "100%" }} />
          </PanelSectionRow>
        ))}
        <PanelSectionRow>
          <div style={{ ...NOTE, padding: "4px 0" }}>
            {pins.length ? hintText("all") : "Nothing is pinned yet. Open Browse files and pin the ones you use."}
          </div>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={openBrowse}>Browse files</ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ConnectionButton />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={s.owner.blocked === "window" || s.pairing.open}
                      onClick={(e: MouseEvent) => openPairing(viaOf(e))}>
            Pair a Flipper
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}
