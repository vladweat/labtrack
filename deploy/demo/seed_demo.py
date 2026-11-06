"""Compatibility entrypoint for deployments using the legacy mounted seed path."""

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["seed-demo"]))
