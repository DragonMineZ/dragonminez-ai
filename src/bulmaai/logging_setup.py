import logging
import sys
import colorlog


def setup_logging(level: str = "INFO") -> None:
    handler = colorlog.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "white",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
    )

    root = logging.getLogger()
    for existing_handler in list(root.handlers):
        root.removeHandler(existing_handler)
        existing_handler.close()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)
