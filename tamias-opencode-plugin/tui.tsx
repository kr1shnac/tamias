/**
 * tamias OpenCode TUI plugin.
 *
 * Registers a `session.panel` contribution (tamias.dashboard) and the global
 * `/tamias` command. The panel opens beside the chat, `f` toggles full screen,
 * and Escape closes it. The first open in a process shows a one-line hint so
 * the close key is discoverable without reading the README.
 *
 * Loaded directly from this directory as a TUI-only plugin: the package has no
 * `exports` field, so OpenCode resolves entrypoints from root file names and
 * finds `tui.tsx` (and no `server`/`index`, so no server part is loaded).
 */

import { Plugin } from "@opencode/plugin/tui";
import { TamiasDashboard, PANEL_NAME } from "./src/views/TamiasDashboard";

export default Plugin.define({
  id: "tamias",
  setup(context) {
    const [hint, setHint] = context.storage.memory("tamias.hint", {
      initial: { shown: false },
    });

    context.ui.slot({
      append: "session.panel",
      render: (input) =>
        input.name === PANEL_NAME ? (
          <TamiasDashboard
            input={input}
            renderer={context.renderer}
            theme={context.theme}
            mode={context.themeMode}
            keymap={context.keymap}
          />
        ) : null,
    });

    context.keymap.layer(() => ({
      mode: "global",
      commands: [
        {
          id: "tamias.dashboard.open",
          title: "Open tamias routing dashboard",
          group: "tamias",
          palette: true,
          slash: { name: "tamias" },
          suggested: true,
          run: () => {
            const active = context.ui.panel.current();
            if (active?.name === PANEL_NAME) {
              context.ui.panel.close();
              return;
            }

            context.ui.dialog.clear();
            if (!context.ui.panel.open(PANEL_NAME, { presentation: "panel" })) {
              context.ui.toast.show({
                title: "tamias",
                message: "Open a session first, then run /tamias.",
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
                message: "esc closes · f full screen",
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
