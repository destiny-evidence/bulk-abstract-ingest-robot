"""Entry point script for running the toy robot in polling mode."""

import typer

from app import main

if __name__ == "__main__":
    typer.run(main)
