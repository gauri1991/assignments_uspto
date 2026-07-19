"""Built-in machine-learning name classifier — pure-Python inference, no compiled dependency.

The optional ``probablepeople`` backend needs the C extension ``python-crfsuite``, which has no
wheel for the newest CPython (3.13/3.14), so it cannot run there without a compiler. This backend
is the portable alternative: a logistic-regression model over character n-grams, **trained offline**
(see ``scripts/train_classifier.py``) and shipped as a small gzipped-JSON weight file. Inference is
a plain dictionary sum — it runs on any Python (3.12–3.14), on Windows, with no compiler and no
external data.

Features are **binary character n-grams** (word-boundary padded, lengths 2–4) plus a handful of
interpretable token flags; because they are presence/absence, the score is a simple additive sum, so
loading the vocabulary into a ``dict`` and summing the present n-grams' weights is the whole model.
:func:`classify_name_model` wraps that in a deterministic gate that keeps the near-certain cases
(legal-suffix companies, ``LAST, FIRST`` people) on the same fast rules the default classifier uses.
"""
# This module is a companion to ``classify`` and shares its internal rule primitives
# (``_has_company_signal``/``_is_comma_person_form``/``_classify_rules``/``_PERSON_SUFFIXES``) — the
# same ones the training script and the classify tests reuse — so cross-module private access here
# is intentional, not a leak.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import base64
import gzip
import json
import math
import struct
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, cast

from .classify import (
    _PERSON_SUFFIXES,
    EntityType,
    _classify_rules,
    _has_company_signal,
    _is_comma_person_form,
)
from .normalize import clean

_ARTIFACT = "resources/namemodel.json.gz"
_ARTIFACT_FORMAT = "uspto-namemodel"
# Feature-vector order for the interpretable token flags — MUST match the training script and the
# ``token_features`` list stored in the artifact. Kept here as the single source of truth.
TOKEN_FEATURES: tuple[str, ...] = (
    "has_company_token",
    "has_comma_form",
    "tok_eq_1",
    "tok_2_4",
    "has_person_suffix",
)
_NGRAM_SIZES: tuple[int, ...] = (2, 3, 4)


def char_wb_ngrams(cleaned: str) -> set[str]:
    """Word-boundary character n-grams (lengths 2–4) of an already-``clean()``-ed name.

    Each whitespace token is padded with one space on each side, then every window of each length is
    emitted; the **set** is used (binary presence). This exact function is used at both training and
    inference time, so the featurization can never desync — it is the single featurizer.
    """
    grams: set[str] = set()
    for token in cleaned.split():
        padded = f" {token} "
        width = len(padded)
        for n in _NGRAM_SIZES:
            for i in range(width - n + 1):
                grams.add(padded[i : i + n])
    return grams


def token_feature_vector(name: str, cleaned: str) -> list[float]:
    """The interpretable token flags for a name, in :data:`TOKEN_FEATURES` order (0.0/1.0)."""
    tokens = cleaned.split()
    n = len(tokens)
    return [
        1.0 if _has_company_signal(cleaned, tokens) else 0.0,
        1.0 if _is_comma_person_form(name) else 0.0,
        1.0 if n == 1 else 0.0,
        1.0 if 2 <= n <= 4 else 0.0,
        1.0 if any(t in _PERSON_SUFFIXES for t in tokens) else 0.0,
    ]


@dataclass(frozen=True, slots=True)
class _Model:
    """Parsed model: n-gram→weight, token weights (TOKEN_FEATURES order), intercept, thresholds."""

    ngram_weights: dict[str, float]
    token_weights: list[float]
    intercept: float
    t_low: float
    t_high: float


def _resource() -> Any:
    """The importlib.resources traversable for the vendored artifact."""
    return resources.files("uspto_assignments").joinpath(_ARTIFACT)


@cache
def _load_model() -> _Model | None:
    """Parse the vendored artifact once; ``None`` if it is missing or unreadable.

    Mirrors ``classify._load_probablepeople``: any failure resolves to ``None`` so callers fall back
    to rules instead of crashing. The artifact ships with the package, so this normally succeeds.
    """
    try:
        raw = _resource().read_bytes()
        payload = cast("dict[str, Any]", json.loads(gzip.decompress(raw).decode("utf-8")))
        if payload.get("format") != _ARTIFACT_FORMAT:
            return None
        vocab_text = str(payload["vocab"])
        vocab: list[str] = vocab_text.split("\n") if vocab_text else []
        buf = base64.b64decode(payload["weights"])
        weights = struct.unpack(f"<{len(buf) // 2}e", buf)  # little-endian float16, stdlib only
        if len(vocab) != len(weights):
            return None
        ngram_weights: dict[str, float] = {
            ngram: float(w) for ngram, w in zip(vocab, weights, strict=True)
        }
        token_weights: list[float] = [float(w) for w in payload["token_weights"]]
        return _Model(
            ngram_weights=ngram_weights,
            token_weights=token_weights,
            intercept=float(payload["intercept"]),
            t_low=float(payload["t_low"]),
            t_high=float(payload["t_high"]),
        )
    except Exception:  # optional model: any load/parse failure → None → caller uses rules
        return None


def model_available() -> bool:
    """Whether the built-in model artifact is present (cheap; no parsing/side effects)."""
    try:
        return bool(_resource().is_file())
    except (OSError, ModuleNotFoundError):
        return False


def _company_probability(model: _Model, name: str, cleaned: str) -> float:
    """Sigmoid of the additive score; higher → more company-like (positive class = company)."""
    score = model.intercept
    get = model.ngram_weights.get
    for gram in char_wb_ngrams(cleaned):
        score += get(gram, 0.0)
    for value, weight in zip(token_feature_vector(name, cleaned), model.token_weights, strict=True):
        if value:
            score += weight
    return 1.0 / (1.0 + math.exp(-score))


def classify_name_model(name: str) -> EntityType:  # noqa: PLR0911 - one return per gate branch
    """Classify one name via the built-in model, with a rules fast-path and rules fallback.

    Gate (keeps the near-certain cases deterministic regardless of the trained weights):
      1. empty → ``unknown``
      2. a company legal/organization signal → ``company``
      3. the ``LAST, FIRST`` comma form → ``individual``
      4. a single bare token with no company signal → ``unknown`` (brands are not guessed)
      5. otherwise the model decides company / individual / unknown via the abstention band
    Any missing/broken artifact falls back to the pure rules classifier — this never raises.
    """
    cleaned = clean(name)
    if not cleaned:
        return "unknown"
    tokens = cleaned.split()
    if _has_company_signal(cleaned, tokens):
        return "company"
    if _is_comma_person_form(name):
        return "individual"
    if len(tokens) == 1:
        return "unknown"
    model = _load_model()
    if model is None:
        return _classify_rules(name)
    probability = _company_probability(model, name, cleaned)
    if probability >= model.t_high:
        return "company"
    if probability <= model.t_low:
        return "individual"
    return "unknown"
