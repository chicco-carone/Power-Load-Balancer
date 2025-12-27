#!/usr/bin/env python
"""Filter Home Assistant logs for a specific custom integration."""

import argparse
import logging
import sys
from pathlib import Path


def filter_ha_logs(log_file_path: Path, domain: str) -> None:
    """
    Read a Home Assistant log file and print lines matching the domain's logger.

    Args:
        log_file_path: The path to the Home Assistant log file.
        domain: The domain of your custom integration (e.g., 'power_load_balancer').

    """
    logger = logging.getLogger(__name__)
    # The logger name for a custom integration is typically 'custom_components.<domain>'
    target_logger_prefix = f"custom_components.{domain}"
    # We'll look for a pattern like '[custom_components.power_load_balancer'

    try:
        with Path.open(log_file_path, encoding="utf-8") as f:
            for line in f:
                # Check if the line contains the target logger prefix within brackets
                if f"[{target_logger_prefix}" in line:
                    logger.info(
                        line.strip()
                    )  # Log the line, removing leading/trailing whitespace

    except FileNotFoundError:
        logger.exception("Log file not found at %s", log_file_path)
        sys.exit(1)
    except Exception:
        logger.exception("An error occurred: %s")
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Filter Home Assistant logs for a specific custom integration."
    )
    parser.add_argument(
        "log_file_path",
        help="The full path to the Home Assistant log file "
        "(e.g., /config/home-assistant.log).",
    )
    parser.add_argument(
        "-d",
        "--domain",
        default="power_load_balancer",
        help="The domain of your custom integration (default: power_load_balancer).",
    )

    args = parser.parse_args()

    logger = logging.getLogger(__name__)

    # Basic check if the path exists, though open() will also catch FileNotFoundError
    if not Path.exists(args.log_file_path):
        logger.error("Specified log file path does not exist: %s", args.log_file_path)
        sys.exit(1)

    filter_ha_logs(args.log_file_path, args.domain)
