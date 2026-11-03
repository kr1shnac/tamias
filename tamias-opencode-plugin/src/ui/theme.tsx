/**
 * Palette context.
 *
 * The host theme is reactive: switching OpenCode's theme updates the panel
 * without a restart. Components read the palette through `usePalette()` so a
 * single memo re-resolves every token once per theme change.
 */

import { createContext, createMemo, useContext, type JSX } from "solid-js";
import { paletteFor, type Palette, type ThemeLike } from "../utils/palette";

export type { Palette, ThemeLike } from "../utils/palette";

const PaletteContext = createContext<() => Palette>();
const FALLBACK: Palette = paletteFor(undefined);

export interface ThemeProviderProps {
  theme?: ThemeLike;
  mode?: "dark" | "light";
  children: JSX.Element;
}

export function ThemeProvider(props: ThemeProviderProps) {
  const palette = createMemo(() => paletteFor(props.theme, props.mode ?? "dark"));
  return <PaletteContext.Provider value={palette}>{props.children}</PaletteContext.Provider>;
}

export function usePalette(): () => Palette {
  return useContext(PaletteContext) ?? (() => FALLBACK);
}
