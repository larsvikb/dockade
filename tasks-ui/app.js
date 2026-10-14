// SPDX-License-Identifier: Apache-2.0
/* The tasks page's entry: mounts the work list (tasks.js), asks whether the registry
 * answers, and polls while the page is in front: the registry while it answers, and
 * this container's `/running` while it does not.
 *
 * Importing this under node does nothing: `start()` runs only where there is a
 * document, so the unit tests can import every module (tests/test_tasks_ui_js.py).
 */
import {
  mountTasks, probeRegistry, refreshTasks, refreshInbox, registryIsRunning,
  TASKS_POLL_MS,
} from "./tasks.js";

export function start() {
  mountTasks();
  // `visibilityState` is absent in some non-browser hosts; unknown counts as visible.
  const visible = () => document.visibilityState !== "hidden";

  // Once at load, through this container, which answers whether the registry does:
  // nothing here calls it unless it did.
  probeRegistry();
  // Nothing while the page is behind another tab. While the registry answers, the
  // inbox and the task list, in turn, so a registry that stopped meanwhile costs one
  // failed call, not two; while it does not, only this container's `/running`, which
  // answers either way, so a page left open picks the registry up again without a
  // console error.
  setInterval(() => {
    if (!visible()) return;
    if (registryIsRunning()) refreshInbox().then(() => refreshTasks());
    else probeRegistry();
  }, TASKS_POLL_MS);
  // Coming back to the page is when it asks again: at once rather than a poll later,
  // and the registry too if it had stopped answering.
  document.addEventListener("visibilitychange", () => {
    if (!visible()) return;
    if (registryIsRunning()) refreshInbox().then(() => refreshTasks());
    else probeRegistry();
  });
}

if (typeof document !== "undefined") { start(); }
