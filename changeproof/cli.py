import argparse
import json
import sys

from . import __version__
from .invariants.loader import ConfigurationError, load_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="changeproof")
    parser.add_argument("--version", action="version", version=f"ChangeProof {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate an explicit campaign configuration")
    validate.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"status": "configuration_valid", "configuration": config.model_dump()}, indent=2))
    return 0
