"""Distance thresholds belong to the embedding model that made the vectors (memory_core).

A cosine distance means something only in the space of the model it was
measured in: two models put the same two texts at different distances, so a
cutoff tuned on one model lets through too much or too little on another.
Until 2.4.0 the two cutoffs of this package were constants tuned on Google's
`gemini-embedding-2`; the move to EmbeddingGemma-300m computed on our own
machines (decision of 2026-09-29) needs its own values, and an engine may run either.

Each engine says once, at startup, which model makes its vectors
(`use_model`); the readers take `current()`. A model with no measured
thresholds is an error at startup, never a silent borrowing of another
model's numbers.

Calibration of 2026-09-29 (on production data in a throwaway container: 8,046
agent facts from 171 memories, 208,788 pairs of facts of the same memory, 529
pairs the judge had closed; 82 relation clusters). Method: quantile matching,
the EmbeddingGemma value lets through the same fraction of the same pairs as
the Gemini value, checked on what each threshold is for.
- `semantic` (the judge's candidates by meaning, `candidates.for_the_judge`
  stage 1b, and `judge.after_the_vector`): 0.45 on Gemini lets through 68.2%
  of the pairs; the same fraction is 0.667 on EmbeddingGemma with the
  document prompt the stored facts use. The real contradiction is among the
  ten nearest facts 87.3% of the time on Gemini, 87.1% on EmbeddingGemma
  (83.2% with the similarity prompt, which is why facts are stored as
  documents); candidates per fact after the cut 9.91 against 9.93.
- `shortlist` (the relations already known to offer for a new phrase,
  `store._candidates`): 0.65 on Gemini lets through 99.1% of the pairs of
  relations; the same fraction is 0.668 on EmbeddingGemma with the similarity
  prompt; the 12 known aliases all fall inside (farthest 0.197).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union


@dataclass(frozen=True)
class Twin:
    """Cosine distance cutoffs of the fusion of near-duplicates on the write
    (2.8.0, `twins`): the new object's words all in the kept one, or neither
    object's words all in the other."""
    content: float
    different: float


@dataclass(frozen=True)
class Thresholds:
    """Cosine distance cutoffs for one embedding model."""
    semantic: float   # judge candidates by meaning (stage 1b)
    shortlist: float   # vocabulary: known relations offered for a new phrase
    # The fusion of near-duplicates (2.8.0, `twins.py`). None: not measured
    # on this model, and the fusion stays off on it.
    twin: Optional[Twin] = None


GEMINI = Thresholds(semantic=0.45, shortlist=0.65)
# `twin`: cosine 0.93 and 0.95, read on 224 pairs of facts of the same end
# user and subject (2026-10-05; the LongMemEval bench of 180 questions, vectors
# of EmbeddingGemma-300m at full precision). One reader's labels: an estimate,
# to be read again before the fusion is turned on anywhere.
EMBEDDINGGEMMA = Thresholds(semantic=0.67, shortlist=0.67,
                        twin=Twin(content=0.07, different=0.05))

# By family: the preview and the GA Gemini model gave bit-identical vectors
# (config.py, 2026-09-28). EmbeddingGemma is measured at full precision (fp32),
# the model the cloud and korely-agent both run; the 8-bit model of the
# desktop app puts a text a little elsewhere (cosine 0.985 with fp32, one text
# against itself) and has thresholds of its own, measured in desktop/.
# A family is the name without what does not change the space: the preview
# suffix of Gemini, a "models/" path. gemini-embedding-001 or another size of
# EmbeddingGemma are other spaces, and have no thresholds until measured.
_FAMILIES = (
    ("gemini-embedding-2", GEMINI),
    ("embeddinggemma-300m", EMBEDDINGGEMMA),
)


class ModelWithoutThresholds(ValueError):
    """An embedding model whose thresholds were never measured."""


FAMILIES = tuple(prefix for prefix, _ in _FAMILIES)


def family(name: str) -> str:
    """The family of a model name ("gemini-embedding-2",
    "models/gemini-embedding-2-preview", "embeddinggemma-300m", ...), one of
    FAMILIES: the key its thresholds are measured under, here and in the
    engine's own tables of thresholds outside this package."""
    n = (name or "").strip().lower()
    if n.startswith("models/"):
        n = n[len("models/"):]
    for prefix in FAMILIES:
        if n.startswith(prefix):
            return prefix
    raise ModelWithoutThresholds(
        f"no measured distance thresholds for the embedding model {name!r}: "
        "measure them before using it"
    )


def for_model(name: str) -> Thresholds:
    """The thresholds of a model, by its name."""
    return dict(_FAMILIES)[family(name)]


_source: Union[str, Callable[[], str]] = "gemini-embedding-2"


def use_model(name: Union[str, Callable[[], str]]) -> Thresholds:
    """Called by the engine at startup with the model its vectors come from:
    a name, or a function that returns it, read at every use (an engine that
    decides its embedding model per call, like the cloud's `embed_provider()`,
    so the thresholds can never lag behind the vectors). Checked now: an
    unmeasured model fails here, not at the first write."""
    global _source
    thresholds = for_model(name() if callable(name) else name)
    _source = name
    return thresholds


def model() -> str:
    return _source() if callable(_source) else _source


def current() -> Thresholds:
    return for_model(model())
