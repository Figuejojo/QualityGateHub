# Quality Gate Dashboard

A standard-library Python dashboard for monitoring CI quality gates across
workflows. Pipeline results are accepted through an HTTP API, processed by a
background worker, stored in SQLite, and pushed to the browser through
Server-Sent Events.

## Start

From the repository root:

```text
python Dashboard.py
```

Open <http://127.0.0.1:8080>.

Useful options:

```text
python Dashboard.py --port 9000
python Dashboard.py --db :memory: --no-seed
python Dashboard.py --reset
```

## Main Documentation

Read the [Project Guide](docs/PROJECT_GUIDE.md) for the current project
structure, API payloads, database behavior, development rules, and instructions
for adding new checks.

Architecture planning is documented in:

- [Architecture Proposal](docs/ArchDesign/ARCHITECTURE_PROPOSAL.md)
- [UML Design Plan](docs/ArchDesign/UML_DESIGN_PLAN.md)

## Project Shape

- `Dashboard.py` is only the application entry point.
- `src/backend/domain` contains models, policies, validation, and ports.
- `src/backend/application` contains use cases and the background worker.
- `src/backend/infrastructure` contains SQLite, queue, event, and clock adapters.
- `src/backend/interfaces` contains HTTP, presentation, and frontend adapters.
- `src/frontend/index.html` contains the dashboard UI.
- `tools/` contains testing utilities and is outside the application architecture.

The project intentionally uses Python's standard library only.
