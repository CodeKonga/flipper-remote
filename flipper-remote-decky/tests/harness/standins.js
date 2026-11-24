// Stand-ins for what Steam and Decky give a plugin at runtime: React (SP_REACT, SP_JSX), the @decky/ui
// components (DFL) and the loader's API connection. Plain HTML, so the plugin's own logic can be tested in Chrome
// against its real backend. This is not how Steam draws things.
"use strict";
(function () {
  const R = window.React;
  const h = R.createElement;
  window.SP_REACT = R;
  window.SP_REACTDOM = window.ReactDOM;
  // React's own JSX runtime (which Steam provides) treats a fixed list of children (jsxs) as static: no keys needed.
  const withKey = (props, key) => (key === undefined ? props : Object.assign({}, props, { key }));
  const jsx = (type, props, key) => h(type, withKey(props, key));
  const jsxs = (type, props, key) => {
    const rest = Object.assign({}, props);
    const children = rest.children || [];
    delete rest.children;
    return h(type, withKey(rest, key), ...children);
  };
  window.SP_JSX = { jsx, jsxs, Fragment: R.Fragment };

  // ---- harness state (what Steam would decide) ----
  const H = { qamVisible: false, route: null, routes: {}, listeners: {}, toasts: [], subs: new Set(), calls: [],
              modals: [], modalSeq: 0 };
  const changed = () => H.subs.forEach((fn) => fn());
  function useHarness() {
    const [, set] = R.useState(0);
    R.useEffect(() => { const fn = () => set((n) => n + 1); H.subs.add(fn); return () => H.subs.delete(fn); }, []);
    return H;
  }

  // ---- @decky/ui stand-ins ----
  const GAMEPAD_PROPS = ["onButtonDown", "onButtonUp", "onOKButton", "onGamepadFocus", "onGamepadBlur",
                         "onOKActionDescription", "onSecondaryButton", "onSecondaryActionDescription",
                         "onCancelButton", "flow-children", "noFocusRing", "focusable"];
  function domProps(props) {
    const out = {};
    for (const [k, v] of Object.entries(props)) if (!GAMEPAD_PROPS.includes(k) && k !== "children") out[k] = v;
    return out;
  }
  function DialogButton(props) {
    const ref = (el) => { if (el) el.__props = props; };
    return h("button", Object.assign(domProps(props), { ref, type: "button",
                                                        className: ("dlg-button " + (props.className || "")).trim(),
                                                        "data-ok": props.onOKActionDescription || "",
                                                        children: props.children }));
  }
  function ButtonItem(props) {
    return h("div", { className: "row item" + (props.layout === "below" ? " below" : "") },
      props.label ? h("div", { className: "label" }, props.label) : null,
      h(DialogButton, { onClick: props.onClick, disabled: props.disabled, children: props.children }));
  }
  function Dropdown(props) {
    return h("select", {
      className: "dropdown", value: props.selectedOption, disabled: props.disabled, "aria-label": props.menuLabel,
      onChange: (e) => {
        const opt = props.rgOptions.find((o) => String(o.data) === e.target.value);
        if (opt && props.onChange) props.onChange(opt);
      },
    }, props.rgOptions.map((o) => h("option", { key: String(o.data), value: String(o.data) }, o.label)));
  }
  function DropdownItem(props) {
    return h("label", { className: "row item dropdown-item" }, h("span", { className: "label" }, props.label),
      h(Dropdown, props));
  }
  function Focusable(props) {
    return h("div", Object.assign(domProps(props), { className: ("focusable " + (props.className || "")).trim(),
                                                     children: props.children }));
  }
  function PanelSection(props) {
    return h("section", { className: "panel-section" }, props.title ? h("h3", null, props.title) : null,
             h("div", { children: props.children }));
  }
  function PanelSectionRow(props) { return h("div", { className: "panel-row", children: props.children }); }
  function SteamSpinner() { return h("span", { className: "spinner" }, "◌"); }
  // A dialog: Steam's showModal hands the element a closeModal, and calls fnOnClose however it's closed. The B
  // button (harness.pressB) cancels: onCancel, then closeModal.
  function showModal(el, parent, props) {
    const id = ++H.modalSeq;
    const close = () => {
      if (!H.modals.some((m) => m.id === id)) return;
      H.modals = H.modals.filter((m) => m.id !== id);
      changed();
      if (props && props.fnOnClose) props.fnOnClose();
    };
    H.modals = H.modals.concat([{ id, el: R.cloneElement(el, { closeModal: close }), title: props && props.strTitle }]);
    changed();
    return { Close: close, ClosedPromise: new Promise(() => {}), Update() {} };
  }
  function ModalRoot(props) {
    if (!props.closeModal && !props.onCancel) throw new Error("Either closeModal or onCancel should be passed");
    const ref = (el) => { if (el) el.__modal = props; };
    return h("div", { ref, className: "modal-root", role: "dialog", children: props.children });
  }
  const Navigation = {
    Navigate(path) { H.route = path; changed(); },
    NavigateBack() { H.route = null; changed(); },
    CloseSideMenus() { H.qamVisible = false; changed(); },
  };
  window.DFL = { DialogButton, ButtonItem, Dropdown, DropdownItem, Focusable, PanelSection, PanelSectionRow,
                 SteamSpinner, Navigation, showModal, ModalRoot, staticClasses: { Title: "qam-title" } };

  // ---- the loader's API connection (what @decky/api connects to) ----
  async function callBackend(name, args) {
    H.calls.push([name, args]);
    const res = await fetch("/call", { method: "POST", body: JSON.stringify({ name, args }) });
    const body = await res.json();
    if (body.exception) throw new Error(body.exception);
    return body.result;
  }
  const api = {
    _version: 2,
    call: (name, ...args) => callBackend(name, args),
    callable: (name) => (...args) => callBackend(name, args),
    addEventListener(event, fn) { (H.listeners[event] = H.listeners[event] || []).push(fn); return fn; },
    removeEventListener(event, fn) { H.listeners[event] = (H.listeners[event] || []).filter((f) => f !== fn); },
    routerHook: {
      addRoute(path, component) { H.routes[path] = component; changed(); },
      removeRoute(path) { delete H.routes[path]; changed(); },
    },
    toaster: { toast(t) { H.toasts.push(String(t.body)); changed(); return { data: t, dismiss() {} }; } },
    useQuickAccessVisible() { return useHarness().qamVisible; },
  };
  window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit = { connect: () => api };

  let since = 0;
  async function pollEvents() {
    try {
      const res = await fetch(`/events?since=${since}`);
      const body = await res.json();
      since = body.next;
      for (const [event, args] of body.events) for (const fn of H.listeners[event] || []) fn(...args);
    } catch (e) { /* server going away at the end of a test */ }
    setTimeout(pollEvents, 80);
  }

  // ---- the screen: the Quick Access panel (kept mounted, like Steam does) and the page a route shows ----
  function Screen({ plugin }) {
    const s = useHarness();
    const Page = s.route ? s.routes[s.route] : null;
    return h("div", { className: "screen" },
      h("aside", { id: "qam", className: "qam", style: { display: s.qamVisible ? "block" : "none" } },
        h("div", { className: "qam-head" }, plugin.titleView, h("span", { className: "qam-icon" }, plugin.icon)),
        plugin.content),
      h("main", { id: "page", className: "page" }, Page ? h(Page) : h("p", { className: "home" }, "Steam home")),
      h("div", { id: "modals" }, s.modals.map((m) => h("div", { key: m.id, className: "modal-layer" }, m.el))),
      h("ul", { id: "toasts" }, s.toasts.map((t, i) => h("li", { key: i }, t))));
  }

  const gp = (button) => ({ detail: { button, is_repeat: false, source: 1 } });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  window.harness = {
    H,
    start(definePluginResult) {
      const plugin = definePluginResult();
      H.plugin = plugin;
      ReactDOM.createRoot(document.getElementById("root")).render(h(Screen, { plugin }));
      pollEvents();
    },
    showQAM(on) { H.qamVisible = on; if (on) H.route = H.route; changed(); },
    goHome() { H.route = null; changed(); },
    // The A button on a focused button: Steam sends button down/up, and a click (on press or on release).
    async pressA(el, holdMs = 60, clickOn = "up") {
      const p = el.__props;
      p.onGamepadFocus && p.onGamepadFocus({ target: el });
      p.onButtonDown && p.onButtonDown(gp(1));
      if (clickOn === "down" && p.onClick) p.onClick(new MouseEvent("click"));
      await sleep(holdMs);
      p.onButtonUp && p.onButtonUp(gp(1));
      if (clickOn === "up" && p.onClick) p.onClick(new MouseEvent("click"));
    },
    // The B button on a dialog: Steam's dialog calls onCancel, then closes.
    pressB() {
      const roots = document.querySelectorAll(".modal-root");
      const props = roots.length ? roots[roots.length - 1].__modal : null;
      if (!props) return false;
      if (props.onCancel) props.onCancel();
      if (props.closeModal) props.closeModal();
      return true;
    },
    dismount() { H.plugin.onDismount(); },
  };
})();
