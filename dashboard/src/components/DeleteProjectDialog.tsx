import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Modal, ModalActions } from "@/components/Modal";
import {
  projectsApi,
  errorMessage,
  type AgentProject,
  type ProjectDeleteResult,
} from "@/lib/agentsApi";

// Deleting a project, in two roads, and the order they are offered in is the
// point of this file.
//
// The first step is the one you get by clicking Delete: the project goes and
// what it held MOVES to Default. It can be undone in every sense that matters,
// because nothing anybody remembered is gone.
//
// Erasing is a separate question, asked second and only from inside this
// dialog: the second step names what is destroyed and wants the project's name
// typed before the button works. The irreversible road must not be the one you
// take by accident, and one confirm() box cannot mean both.
type Step = "move" | "erase";

export function DeleteProjectDialog({
  project,
  onClose,
  onDeleted,
}: {
  project: AgentProject;
  onClose: () => void;
  onDeleted: (result: ProjectDeleteResult) => void;
}) {
  const [step, setStep] = useState<Step>("move");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const nameMatches = typed.trim() === project.name;

  const submit = async (erase: boolean) => {
    if (erase && !nameMatches) return;
    setErr(null);
    setBusy(true);
    try {
      onDeleted(await projectsApi.remove(project.id, { erase }));
    } catch (e) {
      // The route answers a sentence for the cases it refuses (the Default
      // project, an unknown id); anything else is ours.
      setErr(errorMessage(e) ?? "Something went wrong. Nothing was deleted; try again.");
      setBusy(false);
    }
  };

  return (
    <Modal title={`Delete "${project.name}"`} onClose={onClose}>
      {step === "move" ? (
        <>
          <p className="mb-2 text-sm">
            The project is deleted and what it remembered moves to the{" "}
            <span className="font-medium">Default</span> project: its API keys,
            memories, facts, entity aliases and learned relations. No memory
            or fact is erased; a fact Default already holds merges into it.
          </p>
          <p className="mb-2 text-sm">
            Its webhook endpoints do not move: they are deleted, with their
            signing secrets and every delivery still waiting or to be retried.
            Make them again on the Webhooks page if Default should send them
            events.
          </p>
          <p className="mb-4 text-sm text-muted-foreground">
            The project itself is gone for good. Its keys keep working, now
            inside Default, so a running agent does not stop. Apps connected to
            it over OAuth (ChatGPT, Claude) are disconnected instead: they were
            allowed into this project only.
          </p>

          <ModalActions
            error={err}
            onCancel={onClose}
            confirmLabel={busy ? "Deleting…" : "Delete and move to Default"}
            onConfirm={() => submit(false)}
            confirmDisabled={busy}
          />

          <div className="mt-5 border-t border-border pt-4">
            <p className="mb-2 text-xs text-muted-foreground">
              Want the data gone instead of moved? That is a separate, permanent
              step.
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setErr(null);
                setTyped("");
                setStep("erase");
              }}
              className="rounded-md border border-destructive/40 px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/10 disabled:opacity-50"
            >
              Erase its data instead…
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="mb-3 flex gap-2 rounded-md bg-destructive/10 px-3 py-2.5 text-sm text-destructive">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <p className="mb-1.5 font-medium">
                This permanently erases, and cannot be undone:
              </p>
              <ul className="list-disc space-y-0.5 pl-4">
                <li>
                  every memory in the project, including the ones already
                  forgotten
                </li>
                <li>every fact, including closed ones and their history</li>
                <li>its entity aliases and learned relations</li>
                <li>its /v1/batch jobs, queued or finished</li>
                <li>
                  its webhook endpoints, with their signing secrets and every
                  delivery still waiting
                </li>
              </ul>
            </div>
          </div>
          <p className="mb-4 text-sm text-muted-foreground">
            Its API keys are not erased: they move to Default and keep working,
            except keys confined to end users, which are revoked: the end users
            they name are erased with the project. Apps connected to it over
            OAuth are disconnected.
            An erasure audit record is kept with the counts, never the content.
          </p>

          <label className="mb-1.5 block text-sm font-medium">
            Type <span className="font-mono">{project.name}</span> to confirm
          </label>
          <input
            autoFocus
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit(true)}
            placeholder={project.name}
            aria-label="Project name, to confirm the erasure"
            className="mb-4 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-hidden focus:ring-2 focus:ring-destructive"
          />

          <ModalActions
            error={err}
            cancelLabel="Back"
            onCancel={() => {
              setErr(null);
              setStep("move");
            }}
            cancelDisabled={busy}
            confirmLabel={busy ? "Erasing…" : "Erase permanently"}
            onConfirm={() => submit(true)}
            confirmDisabled={busy || !nameMatches}
            danger
          />
        </>
      )}
    </Modal>
  );
}
