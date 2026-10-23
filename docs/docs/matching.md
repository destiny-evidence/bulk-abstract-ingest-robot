# Enhancement rules

The heart of the robot is a deliberately simple question, asked once per cached record by the match runner (`MatchRunner.should_enhance`, `app/robots/match.py`):

> *Is this cached abstract sufficiently different (or the repository abstract sufficiently absent/broken) that submitting it improves the index without a meaningful risk of overwriting something correct?*

This is **not** deduplication — it is a plausibility filter. Full deduplication and data fusion will live in the proper ingestion flow that eventually replaces this robot.

## How records reach the check

1. The match runner selects the next `REQUEST_BATCH_SIZE` records whose `evaluated` flag is not yet `true` and which were not already ruled out via `has_eligible_abstract = false`.
2. Records whose own abstract is shorter than `MIN_ABSTRACT_LENGTH` are flagged `has_eligible_abstract = false` and dropped — they can never produce a useful enhancement.
3. The remaining records are looked up in the repository **by OpenAlex ID** (`app/util/repository.py`). Every reference the lookup returns is checked independently — if a DOI/OpenAlex ID maps to more than one canonical reference, each one passes or fails on its own merits.

## The decision tree

```mermaid
flowchart TD
    START(["matched cache record<br/>+ repository reference"]) --> A{"cached abstract exists<br/>and ≥ MIN_ABSTRACT_LENGTH?"}
    A -- "no" --> REJ1(["reject<br/><i>nothing worth submitting</i>"])
    A -- yes --> B{"reference has a DESTinY ID?"}
    B -- "no" --> REJ2(["reject<br/><i>defensive check</i>"])
    B -- yes --> C{"publication year within<br/>PUBLICATION_YEAR_TOLERANCE<br/>of any known year?<br/><i>(skipped if either side unknown)</i>"}
    C -- "no" --> REJ3(["reject<br/><i>probably not the same paper</i>"])
    C -- "yes / unknown" --> D{"normalised title within<br/>MAX_TITLE_SLOP of any<br/>known title?<br/><i>(skipped if either side unknown)</i>"}
    D -- "no" --> REJ4(["reject<br/><i>different paper despite<br/>shared identifier</i>"])
    D -- "yes / unknown" --> E{"repository already has a<br/>sufficiently similar abstract<br/>(distance ≤ MAX_ABSTRACT_SLOP)?"}
    E -- yes --> REJ5(["reject<br/><i>we already have it</i>"])
    E -- "no" --> OK(["accept —<br/>request enhancement"])

    style REJ1 fill:#f6d7d7,stroke:#b85450
    style REJ2 fill:#f6d7d7,stroke:#b85450
    style REJ3 fill:#f6d7d7,stroke:#b85450
    style REJ4 fill:#f6d7d7,stroke:#b85450
    style REJ5 fill:#f6d7d7,stroke:#b85450
    style OK fill:#d7e8d0,stroke:#5a8a52
```

### About the comparisons

- Titles and abstracts are **normalised before comparison**: lowercased, all non-`a-z` characters removed. This catches formatting/encoding noise cheaply while keeping the logic honest about being a heuristic.
- The similarity measure is a `difflib.ndiff`-based per-character distance over the normalised strings, not a semantic check. Tuning the slop thresholds quickly becomes a slippery slope, so the thresholds are intentionally blunt (see `MAX_TITLE_SLOP` / `MAX_ABSTRACT_SLOP` in [Configuration](configuration.md)).
- Year and title checks are skipped whenever either side has no information — absence of data is never treated as a mismatch.

## What gets submitted

For every accepted record the enhance runner submits **two enhancements** (see `Repository._records_to_enhancements`):

| Enhancement | Content |
|---|---|
| `AbstractContentEnhancement` | The cached abstract (`process = OTHER`). |
| `RawEnhancement` | The entire raw source record plus known external identifiers and the source export date — nothing is thrown away, so future pipelines can use fields this robot ignores. |

Both are submitted with `RESTRICTED` visibility and a provenance string built from `REPOSITORY_PROVENANCE` and the source database, e.g. `PIK Metadata Cache (SCOPUS)`.

!!! note "Why submit the raw payload even when the abstract is rejected?"
    It isn't — currently raw enhancements accompany accepted abstracts only. The open question of attaching `RawEnhancement`s to *all* plausible matches (not just those with a new abstract) was raised in [issue #7](https://github.com/destiny-evidence/bulk-abstract-ingest-robot/issues/7) and is not implemented.

## Design rationale

The rules were refined while planning the API-wrapper ingestion ([issue #7](https://github.com/destiny-evidence/bulk-abstract-ingest-robot/issues/7), [PR #8](https://github.com/destiny-evidence/bulk-abstract-ingest-robot/pull/8)). Key decisions and accepted trade-offs:

- **Match by identifier only.** DOIs (and OpenAlex IDs) are not perfectly reliable, and production data shows roughly 1 in 500 DOIs held by two unmerged references. The year + title plausibility checks exist precisely so that a wrong identifier match fails the rules instead of getting a wrong abstract.
- **Title similarity was added** compared to the first version of the robot (which only checked year tolerance and abstract mismatch) to reduce false-positive matches.
- **"Different enough" replaces "always submit".** Rather than offering any abstract we have, we only submit when no sufficiently similar abstract exists in the repository already. We deliberately do *not* try to judge which abstract "looks better" (e.g. always preferring the longer one) — that decision belongs to a future best-abstract mechanism.
- **Double submissions are acceptable.** The repository handles duplicate enhancements; the robot does not spend effort preventing them.
- **No new references.** The robot only enhances what already exists in the index.
