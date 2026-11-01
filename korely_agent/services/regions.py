"""Where a project's memories are read by a model: "eu" or "global".

The one codebase (8 Oct 2026). The hosted product has given each project a
region since 6 Oct: "eu" writes on Scaleway in
Paris, "global" on Gemini. This install had one writer for everything, chosen
at the first login. Now the install keeps one writer per region (the Models
page saves each provider as the writer of its region, services/model_choice)
and a project may choose its region; a project that never chose (NULL, every
project of an install that never chose) follows the install's default
writer, which is the behaviour of before.

The rules (CONTRIBUTING.md, regions):
- the region decides the provider; a project's region is read at every
  write, so a change applies from the next write and no fact is extracted
  again;
- no fallback from one region to the other, ever: a region with no writer,
  or whose writer is resting after a refusal, leaves its memories in the
  queue (services/facts/drain.py claims only the regions that can write);
- `region` in /v1/ping is the label the operator set with REGION, which the
  service cannot check (api/ping.py); where a memory is read by a model is
  `processing_region`, this.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

EU = "eu"
GLOBAL = "global"
REGIONS = (EU, GLOBAL)


class UnknownRegion(ValueError):
    code = "unknown_region"


def checked(region: Optional[str]) -> Optional[str]:
    """A region as stored: "eu", "global", or None (follow the install)."""
    if region is None:
        return None
    r = str(region).strip().lower()
    if r not in REGIONS:
        raise UnknownRegion(f"region is one of {', '.join(REGIONS)}, not {region!r}")
    return r


def of_project(db: Session, tenant_id: UUID, project_id: Optional[UUID]) -> Optional[str]:
    """The region a write of this project goes to: the project's own, else
    the install's default writer's; None when neither exists (no writer)."""
    from ..core import llm
    from ..db import AgentProject

    own = None
    if project_id is not None:
        own = (db.query(AgentProject.region)
               .filter(AgentProject.id == project_id, AgentProject.tenant_id == tenant_id)
               .scalar())
    return own or llm.default_region()


def processing(db: Session, tenant_id: UUID, project_id: Optional[UUID]) -> Optional[str]:
    """Where this project's memories are read by a model now: its region
    when that region has a writer, else None (they wait). /v1/ping answers
    it as `processing_region` for the project of the key, as the hosted
    product does."""
    from ..core import llm

    region = of_project(db, tenant_id, project_id)
    return region if region is not None and region in llm.regions_with_writer() else None


@contextmanager
def writing_for(db: Session, tenant_id: UUID, project_id: Optional[UUID]):
    """Every model call inside goes to the writer of this project's region."""
    from ..core import llm

    with llm.writing_in(of_project(db, tenant_id, project_id)):
        yield
