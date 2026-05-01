"""Operator-supplied StreamSource implementations.

Drop `<name>.py` files into this directory; the registry in
`sources/__init__.py` auto-imports them and registers any StreamSource
subclasses just before the BrowserUrlSource fallback.

Files whose name starts with `_` (this __init__.py, `_template.py`) are
skipped by the discovery hook.

Everything in this directory is gitignored EXCEPT this file, the README,
and `_template.py`. Your local sources never get committed.
"""
