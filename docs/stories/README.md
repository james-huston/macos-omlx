# Stories (BMAD)

Planned and in-progress work for macos-omlx, tracked as
[BMAD](https://github.com/bmad-code-org/BMAD-METHOD)-style stories: we **plan
first** (write the story), **track progress** inside each story via the
`Tasks / Subtasks` checkboxes, and **track overall progress** in the table below.

## How this works

- One Markdown file per story, named `NNN-short-slug.md` (zero-padded,
  monotonic). Copy [`_TEMPLATE.md`](_TEMPLATE.md) to start a new one.
- Each story carries a **Status** that moves through:
  `Draft → Approved → InProgress → Review → Done`.
- Acceptance Criteria define "done"; Tasks/Subtasks are the checklist we tick off
  as we implement; Dev Notes capture the technical context so the work is
  self-contained.
- Keep the table below in sync whenever a story's status changes.

> Canonical location for this repo is `docs/stories/`. A thin pointer also lives
> at [`docs/improvements/`](../improvements/) for battlemage-style paths.

## Status

| # | Story | Status | Summary |
|---|-------|--------|---------|
| [001](001-bootstrap-macos-omlx.md) | Bootstrap macos-omlx — oMLX model host, owner-scoped LiteLLM sync, benchmarking | Review | Declarative Mac Studio oMLX host sibling to battlemage-llama. Validated on-box. |

_Overall: 0 done / 1 in review / 1 total._
