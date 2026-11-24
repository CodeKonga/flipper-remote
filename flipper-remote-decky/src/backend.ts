import { callable } from "@decky/api";
import type { Listing, Owner, Pin, Settings, Snapshot } from "./state";

// The plugin's main.py answers {ok: true, result} or {ok: false, error}: ordinary problems (the window has the
// Flipper, it's out of range...) come back as a message to show, not as an exception Decky would log.
type Answer<T> = { ok: true; result: T } | { ok: false; error: string };

function method<Args extends unknown[], T>(name: string) {
  const call = callable<Args, Answer<T>>(name);
  return async (...args: Args): Promise<T> => {
    const answer = await call(...args);
    if (!answer || !answer.ok) throw new Error((answer && !answer.ok && answer.error) || "Something went wrong.");
    return answer.result;
  };
}

export const hello = method<[], Snapshot>("hello");
export const heartbeat = method<[], Owner>("heartbeat");
export const closed = method<[], boolean>("closed");
export const listFolder = method<[path: string, refresh: boolean], Listing>("list_folder");
export const send = method<[path: string, signal: string | null, via: string], { job: number | null }>("send");
export const hold = method<[path: string, signal: string | null, action: "start" | "stop", via: string],
  { job: number | null }>("hold");
export const stop = method<[path: string, signal: string | null, via: string], boolean>("stop");
export const setFavorite = method<[path: string, signal: string | null, on: boolean, label: string | null, via: string],
  Pin[]>("set_favorite");
export const setSettings = method<[changes: { folders?: Record<string, string> }, via: string],
  Settings>("set_settings");
export const pair = method<[action: "start" | "choose" | "confirm" | "code" | "cancel", address: string | null,
  remove: boolean | null, code: string | null, via: string], boolean>("pair");
export const connect = method<[], boolean>("connect");
export const disconnect = method<[], boolean>("disconnect");

export const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e)) || "Something went wrong.";
