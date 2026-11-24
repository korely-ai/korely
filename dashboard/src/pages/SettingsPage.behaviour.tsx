// Which of the Settings page's sections that are the service's on the Cloud
// (the writing model, the Google key) are drawn, from what their read holds
// (SettingsPage.tsx, `drawnHere`), and each section as it is drawn in the
// screen's mode: run in node by scripts/check_the_behaviour.mjs (`npm test`),
// drawn with renderToString.
import type { ComponentType } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { ModeContext } from "@/contexts/ModeContext";
import type { Check } from "@/lib/behaviour";
import { ModelProvider, WritingModel, drawnHere } from "@/pages/SettingsPage";

const SERVICES = "set_by_the_service";
const ANSWER = { active: [] };

// A section as the page draws it in the screen's mode. renderToString runs
// no effect, so its read has no answer: `data` is null, as while it loads
// and after it fails (lib/useLoad.ts).
const drawn = (hosted: boolean, Section: ComponentType) =>
  renderToString(
    <ModeContext.Provider value={{ hosted, config: null, keyPrefix: "", mcpPath: "" }}>
      <MemoryRouter>
        <Section />
      </MemoryRouter>
    </ModeContext.Provider>,
  );

export const CHECKS: Record<string, Check> = {
  "on the Cloud only an answer draws them, never a read that failed or is loading": (t) => {
    // useLoad's `data` is null while the read loads and after it fails (a
    // 5xx, the network): a customer saw the superadmin's sections then.
    t.equal(drawnHere(true, null), false, "failed, or loading");
    t.equal(drawnHere(true, SERVICES), false, "refused: the service's");
    t.equal(drawnHere(true, ANSWER), true, "answered: the superadmin's");
  },
  "on an install of one's own they are the person's, a failure said in them": (t) => {
    t.equal(drawnHere(false, null), true, "failed, or loading: the section says it");
    t.equal(drawnHere(false, ANSWER), true);
    t.equal(drawnHere(false, SERVICES), false, "the refusal hides it there too");
  },
  "each section reads the screen's mode: on the Cloud nothing of it before an answer": (t) => {
    // The rule above, as each section applies it (review of 16 Nov 2026: a
    // section that read its mode as an install's drew on the Cloud, and
    // every check of the rule alone still passed).
    t.equal(drawn(true, WritingModel), "", "the writing model, on the Cloud");
    t.equal(drawn(true, ModelProvider), "", "the Google key, on the Cloud");
    t.ok(drawn(false, WritingModel).includes("Writing model"), "an install's, drawn");
    t.ok(drawn(false, ModelProvider).includes("Google key (Gemini)"), "an install's, drawn");
  },
};
