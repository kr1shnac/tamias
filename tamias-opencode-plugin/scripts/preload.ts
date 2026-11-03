/**
 * Test-harness preload that mirrors the OpenCode host.
 *
 * The host installs OpenTUI's Solid transform before importing a runtime
 * plugin. That transform does two things the bare Bun runtime does not:
 *   1. rewrites `solid-js` to its reactive build (Bun's default `node`
 *      condition resolves to the non-reactive `dist/server.js`)
 *   2. compiles plugin TSX with Babel's Solid preset, so dynamic props become
 *      getters and stay reactive
 *
 * Without this the dashboard renders once and never updates, which makes
 * resize and polling look broken when they are not.
 */

import { ensureSolidTransformPlugin } from "@opentui/solid/bun-plugin";

ensureSolidTransformPlugin();
