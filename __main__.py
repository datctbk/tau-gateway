"""Standalone entry point: python3 tau-gateway/__main__.py."""

import asyncio
import sys
from pathlib import Path

# Ensure the tau-gateway package root is importable
_pkg_root = Path(__file__).resolve().parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))


def main():
    """Start the gateway runner as a standalone daemon."""
    from extensions.gateway.runner import GatewayRunner

    runner = GatewayRunner()
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        print("\n[gateway] Shutting down...")


if __name__ == "__main__":
    main()
