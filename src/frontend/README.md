# Frontend

The dashboard page is served as a static asset by
`src/backend/interfaces/frontend.py`, wired by `src/backend/bootstrap.py`.

Keep browser code and styling in this directory. Backend routes should expose
data and events rather than embedding page markup in Python.