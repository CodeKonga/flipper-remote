import { addEventListener, definePlugin, removeEventListener, routerHook } from "@decky/api";
import { staticClasses } from "@decky/ui";
import { Browse } from "./Browse";
import { FlipperRemoteIcon } from "./Icon";
import { closePairingDialog } from "./Pairing";
import { BROWSE_ROUTE, Panel } from "./Panel";
import { onEvent } from "./state";

export default definePlugin(() => {
  // Live updates from the backend: connection, presses, folders, pins, pairing.
  const listener = addEventListener<[event: string, data: any]>("flipper", onEvent);
  routerHook.addRoute(BROWSE_ROUTE, Browse, { exact: true });

  return {
    name: "Flipper Remote",
    titleView: <div className={staticClasses.Title}>Flipper Remote</div>,
    content: <Panel />,
    icon: <FlipperRemoteIcon />,
    onDismount() {
      removeEventListener("flipper", listener);
      routerHook.removeRoute(BROWSE_ROUTE);
      closePairingDialog();
    },
  };
});
