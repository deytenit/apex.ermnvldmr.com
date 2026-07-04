# engine/descriptor.py
"""Declarative action descriptors — successor to the bash action_metadata() heredoc.

Meta drives BOTH the help text and argparse parsing. Exit code 64 (EX_USAGE) on a
usage error, matching the bash engine.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import List


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that exits 64 (EX_USAGE) instead of 2 on a usage error."""

    def error(self, message: str):
        self.print_usage()
        self.exit(64, f"{self.prog}: error: {message}\n")


@dataclass
class Arg:
    """A positional argument. required=False -> optional positional (default None)."""
    name: str
    help: str = ""
    required: bool = True

    def add_to(self, p: argparse.ArgumentParser) -> None:
        if self.required:
            p.add_argument(self.name, help=self.help)
        else:
            p.add_argument(self.name, nargs="?", default=None, help=self.help)


@dataclass
class Opt:
    """An optional value flag, e.g. --tier PATH, with a default."""
    name: str
    help: str = ""
    default: object = None

    def add_to(self, p: argparse.ArgumentParser) -> None:
        p.add_argument(self.name, default=self.default, help=self.help)


@dataclass
class Flag:
    """A boolean flag, e.g. --dry-run (store_true)."""
    name: str
    help: str = ""

    def add_to(self, p: argparse.ArgumentParser) -> None:
        p.add_argument(self.name, action="store_true", help=self.help)


@dataclass
class Rest:
    """Greedy passthrough of all remaining argv (argparse.REMAINDER)."""
    name: str
    help: str = ""

    def add_to(self, p: argparse.ArgumentParser) -> None:
        p.add_argument(self.name, nargs=argparse.REMAINDER, help=self.help)


@dataclass
class Meta:
    summary: str
    args: List[object] = field(default_factory=list)


def build_parser(meta: Meta, prog: str) -> argparse.ArgumentParser:
    p = _Parser(prog=prog, description=meta.summary)
    for a in meta.args:
        a.add_to(p)
    return p
