"""Optional PySide6 desktop interface; importing app.gui does not load Qt."""


def main(argv: list[str] | None = None) -> int:
    """Start the desktop app, importing PySide6 only when requested."""
    from app.gui.window import main as _main

    return _main(argv)


__all__ = ["main"]
