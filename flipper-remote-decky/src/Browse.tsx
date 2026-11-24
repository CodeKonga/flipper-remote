import { CSSProperties, ReactNode, useEffect, useState } from "react";
import { DialogButton, Focusable, SteamSpinner } from "@decky/ui";
import { FaArrowUp, FaFolder, FaSyncAlt, FaTv } from "react-icons/fa";
import * as backend from "./backend";
import { ALL_ROOT, BLOCKED_TEXT, hintText, inBrowse, keyOf, KIND_LIST, kindOf, KINDS, metaText, parentOf,
         pretty } from "./kinds";
import { StatusBlock } from "./Panel";
import { Item, PinButton, PressButton } from "./PressButton";
import { getState, rememberFolder, State, update, usePresence, useStore } from "./state";
import { LINE1, NOTE, scrollIntoView } from "./styles";

// The full-screen Browse page: all of the Flipper's folders (Sub-GHz, Infrared, RFID, NFC, iButton), each file
// with a button to send (or emulate) it and one to pin it to the Quick Access panel.

const PAGE: CSSProperties = { marginTop: "40px", height: "calc(100% - 40px)", overflowY: "auto",
                              padding: "16px 2.8vw 72px", boxSizing: "border-box" };
const TOOL: CSSProperties = { width: "auto", minWidth: "7em", padding: "8px 14px", display: "flex",
                              alignItems: "center", justifyContent: "center", gap: "8px" };
const LIST: CSSProperties = { display: "flex", flexDirection: "column", gap: "8px" };

function startPath(s: State): string {
  const saved = s.settings.folders.all;
  return inBrowse(saved) ? saved : ALL_ROOT;
}

async function load(path: string, refresh: boolean) {
  update((s) => {
    const errors = { ...s.listError };
    delete errors[path];
    s.listError = errors;
    if (refresh) {
      const listings = { ...s.listings };
      delete listings[path];
      s.listings = listings;
    }
  });
  try {
    const listing = await backend.listFolder(path, refresh);
    if (listing && listing.files) {
      update((s) => {
        s.listings = { ...s.listings, [path]: listing };
        const meta = { ...s.meta };
        for (const f of listing.files || []) if (f.meta) meta[f.path] = f.meta;
        s.meta = meta;
      });
    } else if (listing?.offline) {   // nobody to ask the Flipper, and nothing saved from before
      const blocked = getState().owner.blocked;
      update((s) => {
        s.listError = { ...s.listError,
                        [path]: blocked ? BLOCKED_TEXT[blocked] : "Connect to the Flipper to see this folder." };
      });
    }   // otherwise the Flipper is being asked: the folder arrives as a live update
  } catch (e) {
    update((s) => {
      s.listError = { ...s.listError, [path]: backend.messageOf(e) };
    });
  }
}

function crumbs(path: string): string {
  const parts: string[] = ["All"];
  const kind = kindOf(path);
  if (kind) {
    parts.push(KINDS[kind].title);
    parts.push(...path.slice(KINDS[kind].root.length).split("/").filter(Boolean).map(pretty));
  }
  return parts.join("  ›  ");
}

function FolderRow({ name, icon, sub, onOpen }: { name: string; icon: ReactNode; sub?: string; onOpen: () => void }) {
  return (
    <DialogButton onClick={onOpen} onGamepadFocus={scrollIntoView} onOKActionDescription="Open"
                  style={{ textAlign: "left", padding: "10px 12px", display: "flex", alignItems: "center", gap: "10px" }}>
      {icon}
      <span style={{ ...LINE1, flex: 1 }}>{name}</span>
      {sub ? <span style={{ fontSize: "12px", opacity: 0.75 }}>{sub}</span> : null}
    </DialogButton>
  );
}

function FileRow({ item, disabled }: { item: Item; disabled: boolean }) {
  return (
    <Focusable flow-children="row" className="fr-row" style={{ display: "flex", gap: "8px", alignItems: "stretch" }}>
      <PressButton item={item} disabled={disabled} style={{ flex: 1 }} />
      <PinButton item={item} />
    </Focusable>
  );
}

function Contents({ path, open }: { path: string; open: (path: string) => void }) {
  const s = useStore();
  const listing = s.listings[path];
  const err = s.listError[path];
  const blocked = s.owner.blocked === "window";
  if (!listing && err) {
    return (
      <div style={LIST}>
        <div style={{ ...NOTE, fontSize: "14px" }}>{err}</div>
        <Focusable flow-children="row" style={{ display: "flex", gap: "8px" }}>
          <DialogButton style={TOOL} onClick={() => void load(path, true)}>Try again</DialogButton>
          {path !== ALL_ROOT ? (
            <DialogButton style={TOOL} onClick={() => open(ALL_ROOT)}>Back to All</DialogButton>
          ) : null}
        </Focusable>
      </div>
    );
  }
  if (!listing) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: "12px", padding: "16px 0" }}>
        <SteamSpinner style={{ width: "32px", height: "32px" }} />
        <span style={NOTE}>Reading the Flipper's SD card…</span>
      </div>
    );
  }
  const rows: ReactNode[] = [];
  if (listing.all) {   // "All": a folder for each kind
    for (const kind of KIND_LIST) {
      rows.push(<FolderRow key={kind} name={KINDS[kind].title} icon={<FaFolder />}
                           onOpen={() => open(KINDS[kind].root)} />);
    }
  } else if (listing.remote) {   // an Infrared remote, opened: one row per button
    for (const sig of listing.signals || []) {
      const item: Item = { key: keyOf(listing.path, sig.name), path: listing.path, kind: "infrared", signal: sig.name,
                           protocol: sig.protocol, label: sig.name.replace(/_/g, " ") };
      rows.push(<FileRow key={item.key} item={item} disabled={blocked} />);
    }
  } else {
    for (const d of listing.dirs || []) {
      rows.push(<FolderRow key={`d:${d}`} name={pretty(d)} icon={<FaFolder />} onOpen={() => open(`${path}/${d}`)} />);
    }
    for (const f of listing.files || []) {
      if (f.kind === "infrared") {   // a remote opens like a folder
        rows.push(<FolderRow key={f.path} name={pretty(f.name)} icon={<FaTv />}
                             sub={metaText(s.meta[f.path] || f.meta, "infrared")} onOpen={() => open(f.path)} />);
      } else {
        const item: Item = { key: f.path, path: f.path, kind: f.kind, label: pretty(f.name), meta: f.meta };
        rows.push(<FileRow key={f.path} item={item} disabled={blocked} />);
      }
    }
  }
  if (!rows.length) {
    return (
      <div style={NOTE}>
        {listing.remote ? "This remote has no buttons saved." : KINDS[kindOf(path) || "subghz"].empty}
      </div>
    );
  }
  return (
    <Focusable style={LIST}>
      {rows}
      {listing.offline ? <div style={NOTE}>From the last time the Flipper was connected.</div> : null}
    </Focusable>
  );
}

export function Browse() {
  usePresence(true);
  const s = useStore();
  const [path, setPath] = useState<string>(() => startPath(getState()));
  const connected = s.status.state === "connected";

  useEffect(() => {
    void load(path, false);
  }, [path]);
  useEffect(() => {   // just connected: read the folder if it couldn't be read before
    if (connected && !getState().listings[path]) void load(path, false);
  }, [connected]);

  const open = (target: string) => {
    const next = inBrowse(target) ? target : ALL_ROOT;
    setPath(next);
    rememberFolder(next);
  };

  return (
    <div style={PAGE}>
      <div style={{ fontSize: "22px", fontWeight: 700, marginBottom: "4px" }}>Flipper Remote</div>
      <StatusBlock />
      <Focusable flow-children="row" style={{ display: "flex", gap: "8px", alignItems: "center", margin: "8px 0" }}>
        <DialogButton style={TOOL} disabled={path === ALL_ROOT} onClick={() => open(parentOf(path))}>
          <FaArrowUp /> Up
        </DialogButton>
        <DialogButton style={TOOL} onClick={() => void load(path, true)}>
          <FaSyncAlt /> Refresh
        </DialogButton>
      </Focusable>
      <div style={{ ...NOTE, margin: "6px 0 12px" }}>
        {crumbs(path)}. {hintText(kindOf(path) || "all")}
      </div>
      <Contents path={path} open={open} />
    </div>
  );
}
