"""Train the built-in name classifier and write the vendored weight artifact (OFFLINE, dev-only).

Run on this dev machine (Python 3.12, scikit-learn) where the PatentsView reference data lives — it
is NOT part of the shipped package and NOT needed by end users. It regenerates
``src/uspto_assignments/resources/namemodel.json.gz``; that committed artifact is what ships and runs
(pure-Python) on the user's Python 3.14.

Design (see the plan): train company-vs-individual logistic regression on the **gated residual** — the
exact distribution :func:`uspto_assignments.namemodel.classify_name_model` feeds the model, i.e.
multi-token names with no company signal and no ``LAST, FIRST`` comma form. Company examples are
no-signal multi-token organizations; person examples are reconstructed as non-comma ``FIRST LAST`` /
``LAST FIRST`` from the structured individual-name columns (the raw comma forms are handled by the
rules gate before the model). Featurization reuses ``namemodel.char_wb_ngrams`` so it can never
desync from inference.

Usage:
    .venv/bin/python scripts/train_classifier.py
"""

from __future__ import annotations

import base64
import datetime
import gzip
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupShuffleSplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from uspto_assignments.classify import _classify_rules, _has_company_signal, _is_comma_person_form
from uspto_assignments.namemodel import TOKEN_FEATURES, char_wb_ngrams, token_feature_vector
from uspto_assignments.normalize import clean

_REPO = Path(__file__).resolve().parent.parent
_TSV = _REPO / "reference" / "g_assignee_disambiguated.tsv"
_OUT = _REPO / "src" / "uspto_assignments" / "resources" / "namemodel.json.gz"

_MAX_FEATURES = 20_000
_MIN_DF = 5
_WEIGHT_PRUNE = 2e-3  # drop |ngram weight| below this to shrink the artifact
_T_LOW, _T_HIGH = 0.35, 0.65  # abstention band on P(company); inside → "unknown"
_MAX_TOKENS = 8  # ignore absurdly long multi-party strings when building training names


def _residual_company(cleaned: str) -> bool:
    """A company name the model (not the rules gate) must decide: multi-token, no company signal."""
    toks = cleaned.split()
    return 1 < len(toks) <= _MAX_TOKENS and not _has_company_signal(cleaned, toks)


def _load_examples() -> tuple[list[str], list[str], list[int]]:
    """Return (raw_name, cleaned_name, label) lists; label 1 = company, 0 = individual."""
    con = duckdb.connect()
    print("scanning reference TSV …", flush=True)
    orgs = con.execute(
        f"""
        SELECT DISTINCT trim(disambig_assignee_organization) AS org
        FROM read_csv_auto('{_TSV.as_posix()}', delim='\t', header=true, quote='"')
        WHERE nullif(trim(disambig_assignee_organization),'') IS NOT NULL
        """
    ).fetchall()
    people = con.execute(
        f"""
        SELECT DISTINCT trim(disambig_assignee_individual_name_first) AS first,
                        trim(disambig_assignee_individual_name_last)  AS last
        FROM read_csv_auto('{_TSV.as_posix()}', delim='\t', header=true, quote='"')
        WHERE nullif(trim(disambig_assignee_individual_name_last),'') IS NOT NULL
        """
    ).fetchall()

    raw: list[str] = []
    cleaned: list[str] = []
    label: list[int] = []
    seen: dict[str, int] = {}  # cleaned key → label, to drop cross-class conflicts

    def add(name: str, y: int) -> None:
        c = clean(name)
        if not c or _is_comma_person_form(name):  # comma persons are handled by the rules gate
            return
        if len(c.split()) <= 1 or len(c.split()) > _MAX_TOKENS:
            return
        prev = seen.get(c)
        if prev is None:
            seen[c] = y
            raw.append(name)
            cleaned.append(c)
            label.append(y)
        elif prev != y:  # same string labeled both ways → ambiguous, drop it
            seen[c] = -1

    for (org,) in orgs:
        if org and _residual_company(clean(org)):
            add(org, 1)
    for first, last in people:  # reconstruct non-comma forms the model actually sees
        first = (first or "").strip()
        last = (last or "").strip()
        if last and first:
            add(f"{first} {last}", 0)
            add(f"{last} {first}", 0)
        elif last:
            add(last, 0)

    keep = [i for i, c in enumerate(cleaned) if seen.get(c) != -1]
    raw = [raw[i] for i in keep]
    cleaned = [cleaned[i] for i in keep]
    label = [label[i] for i in keep]
    return raw, cleaned, label


def main() -> int:
    if not _TSV.is_file():
        print(f"ERROR: training data not found: {_TSV}", file=sys.stderr)
        return 1
    raw, cleaned, label = _load_examples()
    y = np.array(label)
    print(
        f"residual training set: {len(y):,} names  (company {int(y.sum()):,} / individual {int((y == 0).sum()):,})"
    )

    groups = [c.split()[0] for c in cleaned]  # group by first token → near-dups stay in one split
    splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=0)
    train_idx, test_idx = next(splitter.split(cleaned, y, groups))
    print(f"train {len(train_idx):,} / test {len(test_idx):,} (leakage-safe by first token)")

    vec = CountVectorizer(
        analyzer=char_wb_ngrams, binary=True, min_df=_MIN_DF, max_features=_MAX_FEATURES
    )
    Xtr_ng = vec.fit_transform([cleaned[i] for i in train_idx])
    Xte_ng = vec.transform([cleaned[i] for i in test_idx])
    vocab = vec.get_feature_names_out().tolist()
    print(f"vocab: {len(vocab):,} char n-grams")

    def tokfeat(idx: np.ndarray) -> csr_matrix:
        return csr_matrix(np.array([token_feature_vector(raw[i], cleaned[i]) for i in idx]))

    Xtr = hstack([Xtr_ng, tokfeat(train_idx)]).tocsr()
    Xte = hstack([Xte_ng, tokfeat(test_idx)]).tocsr()

    clf = LogisticRegression(class_weight="balanced", max_iter=2000, C=1.0)
    clf.fit(Xtr, y[train_idx])

    # ---- evaluation on the held-out split (with the abstention band) --------------------------
    proba = clf.predict_proba(Xte)[:, list(clf.classes_).index(1)]
    yte = y[test_idx]
    pred = np.where(proba >= _T_HIGH, 1, np.where(proba <= _T_LOW, 0, -1))
    decided = pred != -1
    acc = float((pred[decided] == yte[decided]).mean())
    abstain = float((~decided).mean())
    # rules baseline on the same held-out residual (rules label these; "unknown" counts as wrong)
    rules_pred = [
        1
        if _classify_rules(raw[i]) == "company"
        else (0 if _classify_rules(raw[i]) == "individual" else -1)
        for i in test_idx
    ]
    rules_acc = float(np.mean([rp == yt for rp, yt in zip(rules_pred, yte)]))
    print(
        f"\nMODEL  held-out accuracy {acc:.3f} on the {1 - abstain:.0%} it decides (abstains {abstain:.0%})"
    )
    print(
        f"RULES  held-out accuracy {rules_acc:.3f} on the SAME residual (rules abstain→'unknown' as wrong)"
    )
    for cls, name in ((1, "company"), (0, "individual")):
        m = yte == cls
        cls_acc = float((pred[m & decided] == cls).mean()) if (m & decided).any() else float("nan")
        print(f"    {name:11s}: model recall {cls_acc:.3f}  (n={int(m.sum()):,})")

    # ---- export the compact artifact ----------------------------------------------------------
    coef = clf.coef_[0]
    ng_w = coef[: len(vocab)]
    tok_w = coef[len(vocab) : len(vocab) + len(TOKEN_FEATURES)]
    keep = np.abs(ng_w) >= _WEIGHT_PRUNE
    vocab_kept = [vocab[i] for i in range(len(vocab)) if keep[i]]
    w_kept = np.asarray(ng_w[keep], dtype="<f2")  # float16
    print(f"\npruned n-grams: {len(vocab):,} → {len(vocab_kept):,} (|w| ≥ {_WEIGHT_PRUNE})")

    payload = {
        "format": "uspto-namemodel",
        "version": 1,
        "created": datetime.date.today().isoformat(),
        "source": "g_assignee_disambiguated.tsv (residual company-vs-individual)",
        "featurizer": {
            "analyzer": "char_wb",
            "ngram_range": [2, 4],
            "binary": True,
            "preprocess": "clean()",
        },
        "classes": ["individual", "company"],
        "vocab": "\n".join(vocab_kept),
        "weights": base64.b64encode(w_kept.tobytes()).decode("ascii"),
        "token_features": list(TOKEN_FEATURES),
        "token_weights": [round(float(w), 6) for w in tok_w],
        "intercept": round(float(clf.intercept_[0]), 6),
        "t_low": _T_LOW,
        "t_high": _T_HIGH,
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_bytes(gzip.compress(json.dumps(payload).encode("utf-8"), mtime=0))
    size = _OUT.stat().st_size
    print(f"wrote {_OUT.relative_to(_REPO)}  ({size / 1024:.0f} KB)")
    if size >= 300 * 1024:
        print("ERROR: artifact exceeds 300 KB budget", file=sys.stderr)
        return 1

    # ---- round-trip check: reload via the runtime loader and re-verify featurizer parity -------
    from uspto_assignments import namemodel

    namemodel._load_model.cache_clear()
    model = namemodel._load_model()
    assert model is not None and len(model.ngram_weights) == len(vocab_kept), "round-trip failed"
    probe = {"XEROX HOLDINGS": None, "YASUSHI KIYOKI": None, "ADVANCED MICRO DEVICES": None}
    for name in probe:
        probe[name] = namemodel.classify_name_model(name)
    print("round-trip OK — sample:", probe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
