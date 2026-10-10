"""Record remote build bytes against immutable Git source; execute no corpus code."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ENTRY = b"# -*- coding: utf-8 -*-\nimport checkwash.zipapp_entry\ncheckwash.zipapp_entry.run()\n"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def git(*args):
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, timeout=120)
    require(result.returncode == 0, "Git inventory command failed: " + repr(args))
    return result.stdout


def safe_name(name):
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts and "\\" not in name,
            "unsafe archive member")


def source_inventory(source):
    require(git("rev-parse", "HEAD").decode().strip() == source, "source SHA mismatch")
    require(not git("diff", "HEAD", "--", "src", "pyproject.toml"), "modified product source")
    entries, proof = {}, {}
    for row in git("ls-tree", "-r", "-z", source + ":src").split(b"\0"):
        if not row:
            continue
        header, path = row.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        name = path.decode("utf-8")
        safe_name(name)
        require(mode in ("100644", "100755") and kind == "blob", "nonregular source member")
        raw = git("cat-file", "blob", oid)
        actual_oid = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        require(actual_oid == oid and (ROOT / "src" / name).read_bytes() == raw, "source bytes mismatch")
        entries[name] = raw
        proof[name] = dict(git_blob=oid, sha256=sha256(raw), bytes=len(raw))
    require(bool(entries), "empty source inventory")
    return entries, proof


def archive_inventory(path, source):
    if path.suffix == ".pyz" or path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            require(len(names) == len(set(names)), "duplicate archive member")
            for item in infos:
                safe_name(item.filename)
                require((item.external_attr >> 16) & 0o170000 != 0o120000, "archive symlink")
            contents = {item.filename: archive.read(item) for item in infos if not item.is_dir()}
        if path.suffix == ".pyz":
            require(set(contents) == set(source) | {"__main__.py"}, "pyz member set differs from Git")
            require(contents["__main__.py"] == ENTRY, "pyz entrypoint mismatch")
            package = {name: raw for name, raw in contents.items() if name != "__main__.py"}
        else:
            package = {name: raw for name, raw in contents.items() if name.startswith("checkwash/")}
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            infos = archive.getmembers()
            names = [item.name for item in infos]
            require(len(names) == len(set(names)), "duplicate sdist member")
            prefixes = {PurePosixPath(name).parts[0] for name in names}
            require(len(prefixes) == 1, "sdist has multiple roots")
            for item in infos:
                safe_name(item.name)
                require(item.isfile() or item.isdir(), "nonregular sdist member")
            contents = {item.name: archive.extractfile(item).read() for item in infos if item.isfile()}
            prefix = next(iter(prefixes)) + "/src/"
            package = {name[len(prefix):]: raw for name, raw in contents.items()
                       if name.startswith(prefix + "checkwash/")}
    else:
        raise ValueError("unreviewed artifact format")
    expected = {name: raw for name, raw in source.items() if name.startswith("checkwash/")}
    require(package == expected, "artifact package bytes differ from Git source")
    return {name: dict(sha256=sha256(raw), bytes=len(raw)) for name, raw in sorted(contents.items())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", choices=("source", "wheel", "pyz"), required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    args = parser.parse_args()
    source = os.environ["CANDIDATE_SOURCE"]
    receipt = dict(schema_version=1, status="failed", source_commit=source,
                   distribution=args.distribution, run_id=os.environ["GITHUB_RUN_ID"],
                   attempt=os.environ["GITHUB_RUN_ATTEMPT"], python=platform.python_version(),
                   builder="tools/release_inventory.py", builder_sha256=sha256(Path(__file__).read_bytes()),
                   errors=[], artifacts=[])
    try:
        entries, proof = source_inventory(source)
        receipt.update(source_tree=git("rev-parse", source + "^{tree}").decode().strip(),
                       src_tree=git("rev-parse", source + ":src").decode().strip(), source_members=proof)
        qualification_raw = args.qualification.read_bytes()
        qualification = json.loads(qualification_raw)
        require(qualification["source_commit"] == source and qualification["status"] == "passed",
                "exact-source assertion qualification did not pass")
        receipt["qualification"] = dict(path=args.qualification.name, sha256=sha256(qualification_raw))
        for artifact in args.artifact:
            raw = artifact.read_bytes()
            receipt["artifacts"].append(dict(name=artifact.name, sha256=sha256(raw), bytes=len(raw),
                                            members=archive_inventory(artifact, entries)))
        require(len(args.artifact) == {"source": 0, "wheel": 2, "pyz": 1}[args.distribution],
                "unexpected distribution artifact count")
        if args.distribution != "source":
            qualified = [item for item in receipt["artifacts"]
                         if item["sha256"] == qualification["artifact_sha256"]]
            require(len(qualified) == 1, "delivered artifact differs from qualification")
        receipt["status"] = "passed"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            zipfile.BadZipFile, tarfile.TarError) as error:
        receipt["errors"].append(repr(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("status", "source_commit", "errors")}))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
