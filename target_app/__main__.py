"""Entry point: `python -m target_app --port 8001 --tenant a`."""

import argparse

import uvicorn
from dotenv import load_dotenv

from target_app.app import create_app
from target_app.tenants import TENANTS


def main() -> None:
    parser = argparse.ArgumentParser(description="Meridian Core mock legacy banking console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--tenant", choices=sorted(TENANTS), default="a")
    args = parser.parse_args()
    load_dotenv()
    uvicorn.run(create_app(args.tenant), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
