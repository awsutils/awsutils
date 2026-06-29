"""Command runner used by the AWS CLI alias."""

import argparse


def main():
    parser = argparse.ArgumentParser(prog="aws utils")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hello", help="Print a friendly greeting.")

    args = parser.parse_args()
    if args.command == "hello":
        print("Hello from aws utils!")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
