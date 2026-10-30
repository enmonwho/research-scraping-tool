"""Entry point for the optional PySide6 desktop interface."""

from app.gui.window import main as _main


def main(argv: list[str] | None = None) -> int:
    """Start the desktop interface."""
    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
