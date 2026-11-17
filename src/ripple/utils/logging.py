from __future__ import annotations

import logging
import sys

_HANDLER_NAME = "ripple-stderr"


def configure_logging(level: str = "INFO") -> None:
    logger = logging.getLogger("ripple")
    resolved = getattr(logging, level.upper(), None)
    logger.setLevel(resolved if isinstance(resolved, int) else logging.INFO)

    if any(h.get_name() == _HANDLER_NAME for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
