// Where an address no route of the screen has goes (App.tsx,
// UnknownAddress): home, but only once the screen knows every route the
// signed-in account has. The routes of the Cloud's parts come from the
// server's `parts` and from a chunk the screen asks then (lib/cloud.ts,
// useScreenParts): sending a part's address home before they are in sent
// the account served it home on a reload, on a link through the login and
// on a source's return (the review of 13 Nov 2026).

/** "lost" on the address of a source's return that the start could not
 *  finish (`lost`, lib/returns.ts lostAt: the parts' chunk did not come),
 *  which the screen says there instead of going anywhere; else "wait"
 *  while the account's routes are not all known (`ready` false), then
 *  "home". */
export function unknownAddress(ready: boolean, lost: boolean): "lost" | "wait" | "home" {
  if (lost) return "lost";
  return ready ? "home" : "wait";
}
