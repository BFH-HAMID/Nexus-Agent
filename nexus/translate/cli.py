"""``nexus translate`` - translate text, detect a language, list languages."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("translate", help="translate text with a local agent; glossary and placeholders protected (opt-in)")
    parser.add_argument("action", choices=["status", "text", "detect", "languages"])
    parser.add_argument("values", nargs="*", help="text: the text to translate, or - to read standard input")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--to", default="", help="target language code or name (default: default_target)")
    parser.add_argument("--from", dest="source", default="", help="source language (default: detected)")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def command(args) -> int:
    from . import detect, status
    from .core import Translator, render

    workspace = Path(args.workspace).resolve()

    def show(value, lines) -> None:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str) if args.json else "\n".join(lines))

    try:
        if args.action == "status":
            info = status(workspace)
            show(info, [f"{key}: {value}" for key, value in info.items()])
            return 0
        text = " ".join(args.values)
        if text == "-":
            text = sys.stdin.read()
        if args.action == "detect":  # offline; works while the translator is off
            found = detect.detect(text)
            show(found, [f"{detect.name_of(found['language']) if found['language'] != 'und' else 'unknown'} "
                         f"({found['language']}), {found['script'] or 'no'} script, confidence {found['confidence']}"
                         + (", mixed" if found["mixed"] else "")])
            return 0
        translator = Translator(workspace)
        if args.action == "languages":
            rows = translator.languages()
            show(rows, [f"{row['code']}  {row['name']}" for row in rows])
            return 0
        result = translator.translate(text, args.to, source=args.source)
        show(result, [render(result)])
    except (ValueError, OSError) as exc:
        print(f"translate: {exc}", file=sys.stderr)
        return 2
    return 0
