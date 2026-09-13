"""Standalone runner — deploy/inspect pages without Hermes.

    INTERACT_BACKEND=pipa python -m interact demo

Open the printed URL in a browser: edits persist in localStorage and, outside Telegram, the submit
buttons show the payload that would be sent to the bot.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from .cli import handle, setup
from .service import InteractService
from .settings import Settings


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=os.getenv("INTERACT_LOG", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m interact", description="PluginInteract standalone runner")
    setup(parser)
    return handle(InteractService(Settings.load()), parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
