/** The install the README describes with no model at all must still reach
 *  the dashboard: "Skip for now" on the setup page is remembered in this
 *  browser under this key. Its own module so the app's gate reads it without
 *  pulling the setup page into the main chunk (audit of 17 Oct 2026). */
export const SKIPPED = "korely.setup.skipped";
