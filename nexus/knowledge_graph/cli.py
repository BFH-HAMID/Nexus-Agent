"""``nexus kg`` - inspect and curate the personal knowledge graph."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("kg", help="personal knowledge graph: entities, relations, forget (opt-in)")
    parser.add_argument("action", choices=["status", "add", "ingest", "about", "path", "search", "alias", "forget",
                                           "clear", "export", "verify"])
    parser.add_argument("values", nargs="*", help="add: SUBJECT PREDICATE OBJECT | about/forget: NAME | path: A B")
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--file", help="ingest: read text from this workspace file")
    parser.add_argument("--unverified", action="store_true", help="ingest: store as unverified (untrusted source)")
    parser.add_argument("--confirm", default="", help="clear: type CLEAR GRAPH")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def _print(args, value, lines) -> None:
    if args.json:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    else:
        for line in lines:
            print(line)


def command(args) -> int:
    from . import policy, status
    from .graph import KnowledgeGraph

    workspace = Path(args.workspace).resolve()
    try:
        if args.action == "status":
            info = status(workspace)
            _print(args, info, [f"{key}: {value}" for key, value in info.items()])
            return 0
        policy.require(workspace)
        graph = KnowledgeGraph(workspace)
        values = list(args.values)
        if args.action == "add":
            if len(values) != 3:
                raise ValueError("Usage: nexus kg add SUBJECT PREDICATE OBJECT")
            fact = graph.add_fact(*values, source_kind="user", source_ref="cli")
            _print(args, fact, [f"{'added' if fact['created'] else 'merged'}: {fact['subject']} → "
                                f"{fact['predicate']} → {fact['object']}"])
        elif args.action == "ingest":
            if args.file:
                from ..workbench import safe_path

                text = safe_path(workspace, args.file).read_text(encoding="utf-8", errors="replace")[:200_000]
                ref = f"file:{args.file}"
            else:
                text, ref = " ".join(values) or sys.stdin.read(200_000), "cli"
            trusted = not args.unverified and not args.file
            result = graph.ingest(text, source_kind="user" if trusted else "file", source_ref=ref, trusted=trusted)
            _print(args, result, [f"added {result['added']}, merged {result['merged']}, skipped {len(result['skipped'])}"]
                   + [f"  skipped: {row['reason']}" for row in result["skipped"]])
        elif args.action == "about":
            found = graph.about(" ".join(values))
            _print(args, found, [graph.sentence(f) for f in found["facts"]] or ["Nothing known."])
        elif args.action == "path":
            if len(values) != 2:
                raise ValueError("Usage: nexus kg path A B (quote names with spaces)")
            chain = graph.path(values[0], values[1])
            _print(args, chain, [graph.sentence(f) for f in chain] or ["No link found."])
        elif args.action == "search":
            hits = graph.search(" ".join(values))
            _print(args, hits, [graph.sentence(f) for f in hits] or ["No match."])
        elif args.action == "alias":
            if len(values) != 2:
                raise ValueError("Usage: nexus kg alias NAME ALIAS")
            out = graph.alias(values[0], values[1])
            _print(args, out, [f"{out['entity']}: aliases {', '.join(out['aliases'])}"])
        elif args.action == "forget":
            out = graph.forget(" ".join(values))
            _print(args, out, [f"removed {out['removed_relations']} relation(s), {out['removed_entities']} entity(ies)"])
        elif args.action == "clear":
            if args.confirm != "CLEAR GRAPH":
                raise ValueError('Refused: pass --confirm "CLEAR GRAPH" to delete the whole graph')
            out = graph.clear()
            _print(args, out, [f"cleared {out['removed_relations']} relation(s)"])
        elif args.action == "export":
            print(json.dumps(graph.export(), indent=2, ensure_ascii=False))
        else:
            report = graph.verify()
            _print(args, report, [f"ok: {report['ok']} ({report['entities']} entities, {report['relations']} relations)"])
            return 0 if report["ok"] else 1
    except (ValueError, OSError) as exc:
        print(f"nexus kg: {exc}", file=sys.stderr)
        return 2
    return 0
