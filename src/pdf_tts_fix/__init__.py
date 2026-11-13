"""Make the text copied out of a PDF friendlier for text-to-speech."""

__version__ = "0.1.0"

from .rewrite import Report, process

__all__ = ["Report", "__version__", "process"]
