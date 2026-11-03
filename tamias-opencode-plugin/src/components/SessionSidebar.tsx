/**
 * Session sidebar.
 *
 * tamias derives one session id per conversation, so the newest metered
 * session is usually the chat that is running now. The dashboard opens on it;
 * this list steps back through older sessions that are still inside the
 * panel's newest-row window. Passthrough rows are excluded by the parent:
 * they are count-only and would otherwise bury real traffic.
 *
 * Rows are selectable by mouse and by the panel's j/k + enter keys. The cursor
 * is a separate marker from the selected scope, so moving the cursor never
 * changes what the table shows until the user confirms it.
 */

import { For } from "solid-js";
import { clip, truncateSessionId, when } from "../utils/format";
import { usePalette } from "../ui/theme";

export interface SessionEntry {
  id: string;
  count: number;
  /** ISO timestamp of the newest row in this session. */
  last: string;
}

export interface SessionSidebarProps {
  sessions: readonly SessionEntry[];
  /** null means "all sessions". */
  scope: string | null;
  /** Newest metered session, badged as the live one. */
  newest: string | null;
  /** Index into [all, ...sessions]. */
  cursor: number;
  width: number;
  onCursor: (index: number) => void;
  onSelect: (scope: string | null) => void;
}

export function SessionSidebar(props: SessionSidebarProps) {
  const palette = usePalette();
  const total = () => props.sessions.reduce((sum, session) => sum + session.count, 0);
  const inner = () => Math.max(8, props.width - 3);
  const idWidth = () => Math.max(4, inner() - 13);

  const label = (session: SessionEntry) =>
    ` ${when(session.last)} ${session.id === props.newest ? "●" : " "} ${truncateSessionId(
      session.id,
      idWidth(),
    )} ${session.count}`;

  return (
    <box
      width={props.width}
      flexShrink={0}
      flexDirection="column"
      border={["right"]}
      borderColor={palette().border}
      paddingRight={1}
      overflow="hidden"
    >
      <text wrapMode="none" fg={palette().muted}>{clip("sessions", inner())}</text>

      <text
        wrapMode="none"
        fg={props.scope === null ? palette().text : palette().muted}
        bg={
          props.cursor === 0
            ? palette().selected
            : props.scope === null
              ? palette().panelHigh
              : undefined
        }
        onMouseUp={() => {
          props.onCursor(0);
          props.onSelect(null);
        }}
      >
        {clip(`${props.cursor === 0 ? "▸" : " "} all sessions ${total()}`, inner())}
      </text>

      <For each={props.sessions}>
        {(session, index) => {
          const position = () => index() + 1;
          return (
            <text
              wrapMode="none"
              fg={session.id === props.scope ? palette().text : palette().muted}
              bg={
                props.cursor === position()
                  ? palette().selected
                  : session.id === props.scope
                    ? palette().panelHigh
                    : undefined
              }
              onMouseUp={() => {
                props.onCursor(position());
                props.onSelect(session.id);
              }}
            >
              {clip(
                `${props.cursor === position() ? "▸" : " "}${label(session)}`,
                inner(),
              )}
            </text>
          );
        }}
      </For>

      {props.sessions.length === 0 ? (
        <text wrapMode="none" fg={palette().faint}>{clip("no metered sessions", inner())}</text>
      ) : null}
    </box>
  );
}
