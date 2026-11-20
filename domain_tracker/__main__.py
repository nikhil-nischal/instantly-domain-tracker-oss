"""Allow `python -m domain_tracker` to invoke the CLI."""

from .main import cli

if __name__ == "__main__":
    cli()
