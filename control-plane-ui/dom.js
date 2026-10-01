// SPDX-License-Identifier: Apache-2.0
/* What more than one surface needs to build markup: `esc`, for the parts of the page
 * rendered from an HTML string, whose values are often agent- or server-authored.
 *
 * No DOM at import, so the unit tests import this file under node
 * (tests/test_control_plane_ui_js.py).
 */

export const esc = s => (s ?? "").toString().replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
