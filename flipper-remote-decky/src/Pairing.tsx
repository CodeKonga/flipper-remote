import { CSSProperties, ReactNode, useEffect, useRef } from "react";
import { DialogButton, Focusable, ModalRoot, Navigation, showModal, SteamSpinner } from "@decky/ui";
import { FaBackspace } from "react-icons/fa";
import { chooseFlipper, closePairing, codeKey, confirmPairing, getState, startPairing, usePresence,
         useStore } from "./state";
import { ACTIVE } from "./styles";

// "Pair a Flipper": the button at the bottom of the panel opens this dialog, which runs the app's own pairing with
// the window's steps and words. A pairing that still works is never thrown away; replacing one is only done when
// asked.

const BOX: CSSProperties = { minWidth: "420px", maxWidth: "560px" };
const TITLE: CSSProperties = { fontSize: "24px", fontWeight: 700, margin: "0 0 14px" };
const TEXT: CSSProperties = { fontSize: "16px", lineHeight: "22px", margin: "0 0 10px" };
const ROW: CSSProperties = { display: "flex", gap: "10px", marginTop: "16px" };

// How a press was made, for the log (like the rest of the panel): the controller, touch or the mouse.
export function viaOf(e: unknown): string {
  const ev = (e || {}) as { detail?: unknown; pointerType?: unknown; nativeEvent?: { pointerType?: unknown } };
  if (typeof ev.detail === "object" && ev.detail && (ev.detail as { button?: number }).button === 1) {
    return "controller:A";
  }
  const type = ev.pointerType ?? (ev.nativeEvent ? ev.nativeEvent.pointerType : undefined);
  return typeof type === "string" && type ? type : "click";
}

let dialog: { close: () => void } | null = null;

export function openPairing(via: string) {
  if (dialog || getState().pairing.open) return;
  startPairing(via);
  Navigation.CloseSideMenus();   // the dialog is the only thing on screen, like the window's
  const shown = showModal(<PairingDialog />, undefined, {
    strTitle: "Pair a Flipper",
    bHideMainWindowForPopouts: false,
    fnOnClose: () => {   // closed some other way (the B button): stop the pairing if it's still going
      dialog = null;
      closePairing(true, "controller:B");
    },
  });
  dialog = { close: () => shown.Close() };
}

export function closePairingDialog() {
  const d = dialog;
  dialog = null;
  if (d) d.close();
}

const P = (props: { children: ReactNode }) => <p style={TEXT}>{props.children}</p>;

function Action({ label, onClick, primary }: { label: string; onClick: (e: unknown) => void; primary?: boolean }) {
  return (
    <DialogButton className={primary ? "fr-action fr-primary" : "fr-action"} onClick={onClick}
                  style={{ width: "auto", minWidth: "8em", flex: primary ? "1 1 auto" : "0 1 auto" }}>
      {label}
    </DialogButton>
  );
}

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "del", "0", "ok"];

function CodePad({ code }: { code: string }) {
  return (
    <>
      <div aria-label={code ? `Typed ${code.split("").join(" ")}` : "Nothing typed yet"}
           style={{ display: "flex", gap: "8px", justifyContent: "center", margin: "6px 0 12px" }}>
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <span key={i} className="fr-slot"
                style={{ width: "34px", height: "42px", lineHeight: "42px", textAlign: "center", fontSize: "26px",
                         fontWeight: 700,
                         borderBottom: `3px solid ${i === code.length ? ACTIVE : "rgba(255, 255, 255, 0.35)"}` }}>
            {code[i] || ""}
          </span>
        ))}
      </div>
      <Focusable style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "8px", margin: "0 auto",
                          maxWidth: "330px" }}>
        {KEYS.map((k) => (
          <DialogButton key={k} className={k === "ok" ? "fr-key fr-ok" : k === "del" ? "fr-key fr-del" : "fr-key"}
                        onClick={() => codeKey(k)} disabled={k === "ok" && code.length !== 6}
                        onOKActionDescription={k === "ok" ? "Pair" : k === "del" ? "Delete" : `Type ${k}`}
                        style={{ minWidth: 0, padding: "12px 0", textAlign: "center", fontSize: "20px",
                                 fontWeight: 600 }}>
            {k === "del" ? <FaBackspace style={{ verticalAlign: "middle" }} /> : k === "ok" ? "OK" : k}
          </DialogButton>
        ))}
      </Focusable>
    </>
  );
}

function focusFirst(root: HTMLElement | null, ...selectors: string[]) {
  if (!root) return;
  for (const sel of selectors) {
    const el = root.querySelector<HTMLElement>(sel);
    if (el && !(el as HTMLButtonElement).disabled) {
      el.focus({ preventScroll: true });
      return;
    }
  }
}

function PairingDialog({ closeModal }: { closeModal?: () => void }) {
  usePresence(true);   // the dialog counts as the panel being open: the plugin keeps the Flipper
  const p = useStore().pairing;
  const box = useRef<HTMLDivElement>(null);
  const nearby = p.devices.filter((d) => d.nearby);

  useEffect(() => {   // the pairing ended (or was never started): the dialog goes too
    if (!p.open) closePairingDialog();
  }, [p.open]);
  useEffect(() => {   // like the window: a Flipper to pick, else the number pad, else the main button
    const active = document.activeElement;
    if (active && box.current && box.current.contains(active) && active.classList.contains("fr-dev")) return;
    focusFirst(box.current, ".fr-dev", ".fr-key", ".fr-primary", ".fr-action");
  }, [p.step, nearby.length > 0]);
  useEffect(() => {   // six digits typed: the next press confirms
    if (p.step === "code" && p.code.length === 6) focusFirst(box.current, ".fr-ok");
  }, [p.step, p.code.length === 6]);
  useEffect(() => {   // a keyboard works too: digits, Backspace and Enter
    if (p.step !== "code") return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (/^\d$/.test(e.key)) codeKey(e.key);
      else if (e.key === "Backspace") codeKey("del");
      else if (e.key === "Enter" && getState().pairing.code.length === 6 &&
               !(document.activeElement && document.activeElement.classList.contains("fr-key"))) codeKey("ok");
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [p.step]);

  const cancel = <Action key="cancel" label="Cancel" onClick={(e) => closePairing(true, viaOf(e))} />;
  const done = <Action key="done" label="Done" primary onClick={(e) => closePairing(false, viaOf(e))} />;
  let body: ReactNode;
  let actions: ReactNode = cancel;
  switch (p.step) {
    case "starting":
    case "searching":
      body = (
        <>
          <P>
            On the Flipper, turn Bluetooth on (Settings → Bluetooth) and go back to its main screen. Turn off your
            phone's Bluetooth while you pair.
          </P>
          {nearby.length ? (
            <>
              <P>Pick your Flipper:</P>
              <Focusable style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                {nearby.map((d) => (
                  <DialogButton key={d.address} className="fr-dev" onClick={(e) => chooseFlipper(d, viaOf(e))}
                                onOKActionDescription="Pair" style={{ textAlign: "left", padding: "10px 14px" }}>
                    <div style={{ fontWeight: 700 }}>{d.name}</div>
                    <div style={{ fontSize: "13px", opacity: 0.8 }}>
                      {d.paired ? `${d.address}. Already paired with this Deck.` : d.address}
                    </div>
                  </DialogButton>
                ))}
              </Focusable>
            </>
          ) : (
            <div style={{ ...TEXT, display: "flex", alignItems: "center", gap: "10px" }}>
              <SteamSpinner style={{ width: "24px", height: "24px" }} />
              <span>Looking for Flippers nearby…</span>
            </div>
          )}
        </>
      );
      break;
    case "testing":
      body = <P>{p.device} is already paired with this Deck. Checking that the pairing still works…</P>;
      break;
    case "works":
      body = <P>{p.device} is already paired with this Deck and it connects fine, so nothing was changed.</P>;
      actions = done;
      break;
    case "confirm":   // asked only when the existing pairing didn't connect; keeping it comes first
      body = (
        <>
          <P>{p.device} is paired with this Deck, but it didn't connect: {p.message || "no answer."}</P>
          <P>
            Pairing again removes the current pairing first. If that pairing still works, keep it and check the
            Flipper is on, nearby, and has Bluetooth on.
          </P>
        </>
      );
      actions = [
        <Action key="keep" label="Keep the pairing" primary onClick={(e) => confirmPairing(false, viaOf(e))} />,
        <Action key="remove" label="Remove and pair again" onClick={(e) => confirmPairing(true, viaOf(e))} />,
      ];
      break;
    case "removing":
      body = <P>Removing the old pairing with {p.device}…</P>;
      break;
    case "pairing":
      body = <P>Connecting to {p.device}. It will show a 6-digit code in a moment…</P>;
      break;
    case "code":
      body = (
        <>
          <P>Type the 6-digit code shown on {p.device}. You have about 30 seconds.</P>
          <CodePad code={p.code} />
        </>
      );
      break;
    case "checking":
      body = <P>Checking the code with {p.device}…</P>;
      break;
    case "done":
      body = <P>Paired with {p.device}. It's ready to use.</P>;
      actions = done;
      break;
    default:   // "error"
      body = <P>{p.message || "Pairing didn't work."}</P>;
      actions = [
        <Action key="close" label="Close" onClick={(e) => closePairing(false, viaOf(e))} />,
        <Action key="again" label="Try again" primary onClick={(e) => startPairing(viaOf(e))} />,
      ];
  }
  return (
    <ModalRoot closeModal={closeModal} onCancel={() => closePairing(true, "controller:B")} bDisableBackgroundDismiss>
      <div ref={box} className="fr-pairing" style={BOX}>
        <div style={TITLE}>Pair a Flipper</div>
        {body}
        <Focusable flow-children="row" style={ROW}>{actions}</Focusable>
      </div>
    </ModalRoot>
  );
}
