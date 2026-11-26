"""Zero-dependency standalone CLI entrypoint for single-repo UI scanning (`ct_ui_scanner.pyz`)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional

from autowork_core.ui_inventory.scanner.repo_scanner import scan_repository, write_snapshot_file


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic CT UI static repository scanner")
    parser.add_argument("--repo-root", required=True, help="Path to local repository working tree")
    parser.add_argument("--repo-id", required=True, help="Logical repository identifier (e.g. MIPPP)")
    parser.add_argument("--branch", default="", help="Optional branch label")
    parser.add_argument("--commit", default="", help="Optional explicit commit SHA override")
    parser.add_argument(
        "--build-list",
        action="append",
        default=[],
        help="Optional BuildList file path(s) for shipping .sln filtering",
    )
    parser.add_argument("--out", required=True, help="Output .snapshot.json.gz or .snapshot.json path")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    if not repo_root.is_dir():
        print(f"ERROR: repo-root does not exist: {repo_root}", file=sys.stderr)
        return 2

    build_lists = [Path(p) for p in args.build_list]
    snapshot = scan_repository(
        repo_root=repo_root,
        repo_id=args.repo_id,
        branch=args.branch,
        commit_override=args.commit,
        external_build_lists=build_lists,
    )
    out_path = Path(args.out)
    digest = write_snapshot_file(snapshot, out_path)
    summary = {
        "repo_id": snapshot["repo_id"],
        "commit": snapshot["commit"],
        "out": str(out_path),
        "sha256": digest,
        "stats": snapshot["stats"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
