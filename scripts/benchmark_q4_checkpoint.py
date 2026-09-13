"""Read-only annual-size persistence probe, never an executable migrated run."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

from microgrid.artifacts import source_tree_hash
from microgrid.dataio import sha256_file
from microgrid.problem.q4_checkpoint import CHECKPOINT_SCHEMA, ledger_witness, restore_ledger
from microgrid.problem.q4_common import atomic_json, jsonable
from microgrid.problem.q4_evidence import LOG_NAMES, prefix_sha256
from microgrid.schemas import InputError


def probe(repo: Path, run: Path, output: Path):
    checkpoint_path = run / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    names = tuple(checkpoint["log_sizes"])
    if not {"contracts", "cost_ledger"} <= set(names) <= set(LOG_NAMES):
        raise InputError("persistence probe invalid log inventory")
    files = [checkpoint_path, *(run / f"{name}.jsonl" for name in names)]
    hashes = {str(path.relative_to(repo)): sha256_file(path) for path in files}
    source = source_tree_hash(repo)
    # Legacy checkpoints have no committed SHA. Bind the observed entire files,
    # require their sizes to match, and label this as post-hoc reconstruction.
    for name in names:
        path = run / f"{name}.jsonl"
        size = checkpoint["log_sizes"][name]
        if path.stat().st_size != size:
            raise InputError("persistence probe requires completed committed logs")
        expected = checkpoint.get("log_sha256", {}).get(name)
        if expected is not None and prefix_sha256(path, size) != expected:
            raise InputError("persistence probe committed hash mismatch")
    historical = checkpoint["ledger"]
    latest = next(reversed(historical["versions"]))
    witness = {
        "case_id": historical["case_id"],
        "bill_count": len(historical["bills"]),
        "contract_day_count": len(historical["versions"]),
        "latest_contract": historical["versions"][latest][-1],
    }
    started = time.perf_counter()
    ledger = restore_ledger(
        run, historical["case_id"], dt.datetime.fromisoformat(checkpoint["time"]), witness
    )
    restore_seconds = time.perf_counter() - started
    if (
        jsonable({"case_id": ledger.case_id, "versions": ledger.versions, "bills": ledger.bills})
        != historical
    ):
        raise InputError("restored ledger differs from historical checkpoint")
    full = {
        **checkpoint,
        "ledger": {"case_id": ledger.case_id, "versions": ledger.versions, "bills": ledger.bills},
    }
    compact = {k: v for k, v in checkpoint.items() if k != "ledger"}
    compact.update(schema_version=CHECKPOINT_SCHEMA, ledger_state=ledger_witness(ledger))
    samples = {"full": [], "compact": []}
    output.mkdir(parents=True, exist_ok=False)
    for _ in range(3):
        for name, payload in (("full", full), ("compact", compact)):
            started = time.perf_counter()
            atomic_json(output / f"{name}.serialization-fixture.json", payload, indent=None)
            samples[name].append(time.perf_counter() - started)
    unchanged = all(sha256_file(repo / name) == digest for name, digest in hashes.items())
    if not unchanged or source_tree_hash(repo) != source:
        raise InputError("persistence probe input or code changed")
    result = {
        "scope": "isolated serialization and atomic replacement, excluding log flush/hash, solver and simulation",
        "fixtures_are_resumable": False,
        "historical_source_hash": checkpoint["source_hash"],
        "probe_source_hash": source,
        "legacy_prefix_evidence": "post-hoc full-file SHA and exact committed sizes; no original prefix SHA claimed",
        "run": str(run.relative_to(repo)),
        "input_hashes": hashes,
        "inputs_unchanged": unchanged,
        "bill_count": len(ledger.bills),
        "contract_count": sum(map(len, ledger.versions.values())),
        "restored_ledger_exact": True,
        "restore_seconds": restore_seconds,
        "seconds": samples,
        "bytes": {
            name: (output / f"{name}.serialization-fixture.json").stat().st_size for name in samples
        },
    }
    atomic_json(output / "comparison.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    print(json.dumps(probe(repo, args.run.resolve(), args.output.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
