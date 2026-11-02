import { useState } from "react";
import { projectsApi, errorCode, type AgentProject, type Region } from "@/lib/agentsApi";
import { Modal, ModalActions } from "@/components/Modal";
import { RegionSelect, RegionHelp } from "@/components/RegionSelect";
import { fieldClass } from "@/components/PageShell";

// Shared "New project" modal — used by the top-left switcher and the Projects
// page, so the create flow stays in one place. A project = name + optional
// description (there is no separate "organization" layer; the account is it).
function createErrorMessage(code: string | null): string {
  switch (code) {
    case "project_name_taken":
      return "A project with that name already exists. Pick another name.";
    case "too_many_projects":
      // projects.MAX_PROJECTS on the server (409, POST /admin/projects).
      return "This account holds 100 projects, archived ones included: delete one first.";
    default:
      return "Something went wrong. Please try again.";
  }
}

export function CreateProjectDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (project: AgentProject) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [region, setRegion] = useState<Region | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setErr("Give your project a name.");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      const project = await projectsApi.create(
        trimmed,
        description.trim() || undefined,
        region,
      );
      onCreated(project);
    } catch (e) {
      setErr(createErrorMessage(errorCode(e)));
      setBusy(false);
    }
  };

  return (
    <Modal title="New project" onClose={onClose}>

        <label className="mb-1.5 block text-sm font-medium">Name</label>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="e.g. Production"
          className={`mb-3 w-full ${fieldClass}`}
        />

        <label className="mb-1.5 block text-sm font-medium">
          Description (optional)
        </label>
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="What this project is for"
          className={`mb-1 w-full ${fieldClass}`}
        />
        <p className="mb-3 text-xs text-muted-foreground">
          A project groups the keys your agents use. You can rename it any time.
        </p>

        <label className="mb-1.5 block text-sm font-medium">Region</label>
        <RegionSelect value={region} onChange={setRegion} />
        <RegionHelp className="mb-4 mt-1 text-xs text-muted-foreground" />

        <ModalActions
          error={err}
          onCancel={onClose}
          confirmLabel={busy ? "Creating…" : "Create project"}
          onConfirm={submit}
          confirmDisabled={busy}
        />
    </Modal>
  );
}
