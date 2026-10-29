"""Owned isolated-interpreter entry point; not a public arbitrary task executor."""
import sys
from pathlib import Path

from ..server import ServerState
from .runtime import execute

if __name__ == '__main__':
    execute(ServerState(Path(sys.argv[1]).resolve(), quiet=True).workbench.studio, sys.argv[2], sys.argv[3])
