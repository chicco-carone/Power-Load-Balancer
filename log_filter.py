#!/usr/bin/env python3

import argparse
import sys
import os


def filter_ha_logs(log_file_path: str, domain: str):
    """
    Reads a Home Assistant log file and prints lines matching the specified domain's logger.

    Args:
        log_file_path: The path to the Home Assistant log file.
        domain: The domain of your custom integration (e.g., 'power_load_balancer').
    """
    # The logger name for a custom integration is typically 'custom_components.<domain>'
    target_logger_prefix = f"custom_components.{domain}"
    # Home Assistant log format usually includes the logger name in brackets, e.g., [custom_components.power_load_balancer]
    # We'll look for a pattern like '[custom_components.power_load_balancer'

    try:
        with open(log_file_path, "r", encoding="utf-8") as f:
            for line in f:
                # Check if the line contains the target logger prefix within brackets
                if f"[{target_logger_prefix}" in line:
                    print(
                        line.strip()
                    )  # Print the line, removing leading/trailing whitespace

    except FileNotFoundError:
        print(f"Error: Log file not found at {log_file_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Filter Home Assistant logs for a specific custom integration."
    )
    parser.add_argument(
        "log_file_path",
        help="The full path to the Home Assistant log file (e.g., /config/home-assistant.log).",
    )
    parser.add_argument(
        "-d",
        "--domain",
        default="power_load_balancer",
        help="The domain of your custom integration (default: power_load_balancer).",
    )

    args = parser.parse_args()

    # Basic check if the path exists, though open() will also catch FileNotFoundError
    if not os.path.exists(args.log_file_path):
        print(
            f"Error: Specified log file path does not exist: {args.log_file_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    filter_ha_logs(args.log_file_path, args.domain)
