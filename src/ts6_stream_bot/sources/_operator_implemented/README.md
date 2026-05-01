# Operator-Implemented Sources

This directory is **gitignored** except for the README, the discovery
package marker (`__init__.py`), and the skeleton (`_template.py`).
Whatever else you drop here stays on your machine.

## Why this directory exists

The upstream `ts6-stream-bot` project deliberately ships no sources for
DRM-protected platforms (Netflix, Disney+, Prime Video, HBO Max, …) and
no AI assistant working on this codebase will write that code for you.
What this directory provides is a **wiring slot**: anything you put here
is auto-registered with the source registry just before the
`BrowserUrlSource` catch-all fallback.

By using this slot you take **sole responsibility** for whatever your
source does, including any legal or platform-ToS implications. This is
your install, on your hardware, for your community.

## Contract

1. Copy `_template.py` to `<name>.py` (no leading underscore — files that
   start with `_` are skipped by the discovery hook).
2. Subclass `ts6_stream_bot.sources.base.StreamSource`.
3. Implement the abstract methods: `can_handle`, `open`, `play`,
   `pause`, `seek`, `close`. (`title()` already returns `self._title`.)
4. Restart the bot. On startup you'll see a `operator_source.registered`
   structlog entry per discovered class.

The new source is registered **before** the `BrowserUrlSource` fallback,
so its `can_handle()` is consulted first for matching URLs.

## What you must NOT ask Claude/Claude Code to do

- Bundle or extract Widevine CDMs.
- Write decryption logic for protected streams.
- Bypass technical protection measures of any kind.

The assistant will refuse those requests. Anything you put in this
directory that does that work, you wrote yourself.

## Quick reference

```text
sources/
└── _operator_implemented/
    ├── __init__.py     # tracked - package marker
    ├── README.md       # tracked - this file
    ├── _template.py    # tracked - skeleton, never auto-registered
    └── <yours>.py      # gitignored - your local sources go here
```
