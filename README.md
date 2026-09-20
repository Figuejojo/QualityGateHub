# Quality Gate Dashboard

## Run

```text
python Dashboard.py
```

`Dashboard.py` is only the application entry point. All backend and frontend
implementation is kept under `src/`.

Open <http://127.0.0.1:8080> in a browser.

## Source layout

- `src/backend/server.py` contains the HTTP server, validation, queue worker,
  and SQLite persistence.
- `src/frontend/index.html` contains the browser UI.
- `src/tools/` contains small integration utilities; `tools/` contains example
    payloads.
- `docs/` contains design references.

The project is intentionally standard-library-only. Later phases can split the
backend services and frontend JavaScript further without changing the HTTP
contract.