"""Run the My Cloud Photos web app."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("app.main:app", host="127.0.0.1", port=8787, reload=False)


if __name__ == "__main__":
    main()
