/**
 * tamias OpenCode TUI plugin.
 *
 * `/tamias` opens a centred pop-up, not a full-screen route or a side panel:
 * the question it answers -- what did that cost, which model ran, is the pool
 * healthy? -- belongs on top of the conversation, not beside it.  Escape
 * closes it, and the first open in a process shows a one-line hint so the key
 * is discoverable without reading the README.
 *
 * The pop-up reads the request log directly and polls tamias' `/limits`
 * endpoint; it never invents provider data when the proxy is not running.
 *
 * Loaded directly from this directory as a TUI-only plugin: the package has no
 * `exports` field, so OpenCode resolves entrypoints from root file names and
 * finds `tui.tsx` (and no `server`/`index`, so no server part is loaded).
 */

import { Plugin } from "@opencode/plugin/tui";
import { TamiasPopup } from "./src/views/TamiasPopup";

export default Plugin.define({
  id: "tamias",
  setup(context) {
    const [hint, setHint] = context.storage.memory("tamias.hint", {
      initial: { shown: false },
    });

    // The dialog API is host-owned: onClose is the only reliable signal that
    // the pop-up is gone (Escape, a click-away or another plugin replacing it).
    let open = false;

    context.keymap.layer(() => ({
      mode: "global",
      commands: [
        {
          id: "tamias.dashboard.open",
          title: "Open the tamias routing pop-up",
          group: "tamias",
          palette: true,
          slash: { name: "tamias" },
          suggested: true,
          run: () => {
            if (open) {
              context.ui.dialog.clear();
              // Do not rely on the host calling onClose here: some hosts clear
              // without firing it, and a stuck flag would make the next
              // /tamias clear an already-empty dialog forever.
              open = false;
              return;
            }

            open = true;
            try {
              context.ui.dialog.show(
                () => (
                  <TamiasPopup
                    renderer={context.renderer}
                    theme={context.theme}
                    mode={context.themeMode}
                  />
                ),
                () => {
                  open = false;
                },
              );
              context.ui.dialog.set({ size: "large", centered: true });
            } catch {
              open = false;
              context.ui.dialog.clear();
              context.ui.toast.show({
                title: "tamias",
                message: "Could not open the pop-up; see the OpenCode log.",
                variant: "warning",
              });
              return;
            }

            if (!hint.shown) {
              setHint((draft) => {
                draft.shown = true;
              });
              context.ui.toast.show({
                title: "tamias",
                message: "esc closes · /tamias opens it again",
                variant: "info",
                duration: 4000,
              });
            }
          },
        },
      ],
    }));
  },
});
