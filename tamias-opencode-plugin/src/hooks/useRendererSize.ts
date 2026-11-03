/**
 * Live terminal size.
 *
 * The TUI host owns the renderer, so the dashboard reads its dimensions through
 * this hook and re-flows on every `resize` event instead of sizing itself once
 * at first render.
 */

import { createSignal, onCleanup } from "solid-js";

export interface RendererLike {
  readonly width: number;
  readonly height: number;
  on(event: "resize", listener: (width: number, height: number) => void): unknown;
  off(event: "resize", listener: (width: number, height: number) => void): unknown;
}

export interface RendererSize {
  width: () => number;
  height: () => number;
}

export function useRendererSize(renderer: RendererLike | undefined): RendererSize {
  const [width, setWidth] = createSignal(renderer?.width ?? 80);
  const [height, setHeight] = createSignal(renderer?.height ?? 24);

  const listener = (nextWidth: number, nextHeight: number) => {
    setWidth(nextWidth);
    setHeight(nextHeight);
  };

  renderer?.on("resize", listener);
  onCleanup(() => {
    renderer?.off("resize", listener);
  });

  return { width, height };
}
