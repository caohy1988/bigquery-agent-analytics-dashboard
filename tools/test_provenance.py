#!/usr/bin/env python3
"""CI regression test for provenance fail-closed behavior (no BigQuery).

Builds a throwaway git repo containing a copy of capture_inventory.py and
asserts:
  * clean tree  -> tool_provenance returns (HEAD commit, content sha256);
  * modified tool -> tool_provenance raises (a dirty tool must never stamp
    evidence with a clean commit);
  * recorded content sha is reproducible from the recorded commit's blob.
"""

import hashlib
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def sh(cwd, *argv):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


def main() -> int:
    src = Path(__file__).parent / "capture_inventory.py"
    errors = []
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        tools = repo / "tools"
        tools.mkdir()
        shutil.copy(src, tools / "capture_inventory.py")
        sh(repo, "git", "init", "-q")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t",
           "add", "-A")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t",
           "commit", "-qm", "init")

        spec = importlib.util.spec_from_file_location(
            "cap", tools / "capture_inventory.py")
        cap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cap)

        script = tools / "capture_inventory.py"
        commit, sha = cap.tool_provenance(str(repo), script)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if commit != head:
            errors.append("clean-tree commit mismatch")
        if sha != hashlib.sha256(script.read_bytes()).hexdigest():
            errors.append("content sha mismatch on clean tree")

        blob = subprocess.run(
            ["git", "-C", str(repo), "show",
             "HEAD:tools/capture_inventory.py"],
            capture_output=True).stdout
        if sha != hashlib.sha256(blob).hexdigest():
            errors.append("content sha not reproducible from HEAD blob")

        with open(script, "a") as fh:
            fh.write("\n# adversarial modification\n")
        try:
            cap.tool_provenance(str(repo), script)
            errors.append("dirty tool was NOT rejected")
        except cap.ProvenanceError:
            pass

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print("provenance tests OK: clean tree verified, sha reproducible from "
          "HEAD blob, dirty tool rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
