"""Compares two tracked roots and figures out what it would take to make them
match each other.

Copies (new/updated files missing on one side) are always considered safe and
reversible, so the "easy sync" button can apply them automatically — but only
*into* a `mirror` root. A `subset` or `watch` root (e.g. a storage-limited
iCloud folder that only ever holds part of the library) is never auto-filled
with what it's missing, and never generates deletion candidates either, since
"not here" is its normal state, not evidence of anything. Deletions from a
`mirror` root are never auto-applied regardless — they require a separate,
explicit confirmation.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import RootConfig

RSYNC_PATH = shutil.which("rsync")


@dataclass
class SyncPlan:
    root_a: str
    root_b: str
    copy_to_a: list[str] = field(default_factory=list)   # rel_paths to copy B -> A
    copy_to_b: list[str] = field(default_factory=list)   # rel_paths to copy A -> B
    delete_from_a: list[str] = field(default_factory=list)  # confirm-only
    delete_from_b: list[str] = field(default_factory=list)  # confirm-only
    conflicts: list[str] = field(default_factory=list)   # differing content both sides
    unverifiable: list[str] = field(default_factory=list)  # one side is a cloud placeholder
    in_sync: int = 0

    @property
    def out_of_sync(self) -> bool:
        return bool(self.copy_to_a or self.copy_to_b or self.delete_from_a or self.delete_from_b or self.conflicts)

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.delete_from_a or self.delete_from_b or self.conflicts)

    def summary(self) -> dict:
        return {
            "in_sync": self.in_sync,
            "copy_to_a": len(self.copy_to_a),
            "copy_to_b": len(self.copy_to_b),
            "delete_from_a": len(self.delete_from_a),
            "delete_from_b": len(self.delete_from_b),
            "conflicts": len(self.conflicts),
            "unverifiable": len(self.unverifiable),
            "out_of_sync": self.out_of_sync,
        }


def _index_all(conn, root_label: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT rel_path, hash, is_placeholder, missing FROM files WHERE root_label=?",
        (root_label,),
    ).fetchall()
    return {r["rel_path"]: dict(r) for r in rows}


def plan_sync(
    conn, root_a: RootConfig, root_b: RootConfig, ignored_promotions: frozenset[str] = frozenset()
) -> SyncPlan:
    idx_a = _index_all(conn, root_a.label)
    idx_b = _index_all(conn, root_b.label)
    plan = SyncPlan(root_a=root_a.label, root_b=root_b.label)

    for rel_path in sorted(set(idx_a) | set(idx_b)):
        a = idx_a.get(rel_path)
        b = idx_b.get(rel_path)

        a_present = a is not None and not a["missing"]
        b_present = b is not None and not b["missing"]
        a_gone = a is not None and a["missing"]
        b_gone = b is not None and b["missing"]

        if a_present and b_present:
            if a["is_placeholder"] or b["is_placeholder"]:
                plan.unverifiable.append(rel_path)
            elif a["hash"] == b["hash"]:
                plan.in_sync += 1
            else:
                plan.conflicts.append(rel_path)
        elif a_present and not b_present:
            if b_gone and root_b.is_deletion_source:
                plan.delete_from_a.append(rel_path)
            elif root_b.mode == "mirror":
                # Never auto-fill a `subset`/`watch` root (e.g. a storage-
                # limited iCloud folder) — it's intentionally partial.
                plan.copy_to_b.append(rel_path)
        elif b_present and not a_present:
            if a_gone and root_a.is_deletion_source:
                plan.delete_from_b.append(rel_path)
            elif root_a.mode == "mirror" and rel_path not in ignored_promotions:
                # Content that showed up on a backup/mirror root but was
                # never on primary -- a promotion candidate (see the
                # Promote section), not something to silently pull in.
                plan.copy_to_a.append(rel_path)
        # both gone or both absent: nothing to do

    return plan


def _copy_one(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if RSYNC_PATH:
        # -a preserves timestamps/permissions, --partial keeps a resumable
        # partial file if interrupted instead of corrupting a half-written copy.
        subprocess.run([RSYNC_PATH, "-a", "--partial", str(src), str(dst)], check=True)
    else:
        shutil.copy2(src, dst)


def promote_one(root_b: RootConfig, root_a: RootConfig, rel_path: str) -> None:
    """The Promote section's single-file 'move to primary' action: the same
    safe/reversible copy as an ordinary mirror-fill, just explicit and one
    file at a time instead of bundled silently into 'easy sync'."""
    _copy_one(root_b.path / rel_path, root_a.path / rel_path)


def execute_sync(
    root_a: RootConfig,
    root_b: RootConfig,
    plan: SyncPlan,
    apply_deletes: bool = False,
    apply_promotions: bool = False,
) -> dict:
    """Applies copy_to_b (filling this backup/subset root from primary) --
    always safe/reversible, so 'easy sync' does it with no extra
    confirmation. copy_to_a (content that showed up on this root but was
    never on primary) is a different, more judgment-laden direction -- it's
    only applied when apply_promotions=True, from the explicit Promote
    section, not bundled into a plain sync. Deletions are similarly only
    applied when apply_deletes=True, from a caller that already got
    separate, specific confirmation for them."""
    results = {"copied_to_a": 0, "copied_to_b": 0, "deleted_from_a": 0, "deleted_from_b": 0, "errors": []}

    if apply_promotions:
        for rel in plan.copy_to_a:
            try:
                promote_one(root_b, root_a, rel)
                results["copied_to_a"] += 1
            except Exception as exc:  # noqa: BLE001 — surface per-file, keep going
                results["errors"].append(f"copy_to_a {rel}: {exc}")

    for rel in plan.copy_to_b:
        try:
            _copy_one(root_a.path / rel, root_b.path / rel)
            results["copied_to_b"] += 1
        except Exception as exc:  # noqa: BLE001
            results["errors"].append(f"copy_to_b {rel}: {exc}")

    if apply_deletes:
        for rel in plan.delete_from_a:
            try:
                (root_a.path / rel).unlink(missing_ok=True)
                results["deleted_from_a"] += 1
            except Exception as exc:  # noqa: BLE001
                results["errors"].append(f"delete_from_a {rel}: {exc}")
        for rel in plan.delete_from_b:
            try:
                (root_b.path / rel).unlink(missing_ok=True)
                results["deleted_from_b"] += 1
            except Exception as exc:  # noqa: BLE001
                results["errors"].append(f"delete_from_b {rel}: {exc}")

    return results


def sync_status_for_all(conn) -> list[dict]:
    """The single-basis model: one root is marked `primary`, and every other
    mirror/subset root is compared against it automatically — no manual
    pairing. Returns [] if no primary root is configured yet."""
    primary_row = db.get_primary_root(conn)
    if primary_row is None:
        return []
    primary = RootConfig.from_row(primary_row)
    ignored = db.ignored_promotion_paths(conn)
    results = []
    for row in db.syncable_roots(conn, exclude_label=primary.label):
        other = RootConfig.from_row(row)
        plan = plan_sync(conn, primary, other, ignored_promotions=ignored)
        results.append({"primary": primary, "root": other, "plan": plan})
    return results


def promotion_candidates(conn) -> list[dict]:
    """Every file that showed up on a backup/mirror root but was never on
    primary (across every such root, not just one pair) -- the Promote
    section's list. Already excludes anything ignored ('cancel once and for
    all'), since plan_sync itself filters those out of copy_to_a."""
    candidates = []
    for status in sync_status_for_all(conn):
        other = status["root"]
        for rel_path in status["plan"].copy_to_a:
            file_row = db.get_file(conn, other.label, rel_path)
            candidates.append(
                {"source_label": other.label, "rel_path": rel_path, "size": file_row["size"] if file_row else None}
            )
    return candidates
