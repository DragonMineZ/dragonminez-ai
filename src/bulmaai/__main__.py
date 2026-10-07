if __package__ != "bulmaai":
    # Cogs load as bulmaai.cogs.*, so starting as src.bulmaai imports every module twice and the
    # webhook server reads an empty route table (every downloads.dragonminez.com URL answers 403).
    raise SystemExit(f"Start with `python -m bulmaai` from src/, not `python -m {__package__}`.")

from .bot import run

if __name__ == "__main__":
    run()
