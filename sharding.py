# -*- coding: utf-8 -*-
"""
sharding.py
==========================================================================
Tiny helper that lets ONE script be run as N parallel shards, each handling
a disjoint slice of the same queue.

Reads (set by the workflow's matrix entry):
    SHARD_COUNT  total number of shards   (default 1 = sharding OFF)
    SHARD_INDEX  this shard, 0-based      (default 0)

With the defaults every function below is a no-op, so running a script by
hand / locally behaves exactly as before.

The slice is decided by a STABLE hash (crc32, not Python's randomised
hash()) of a key you choose, so every shard computes the same partition even
though they start at slightly different moments and see slightly different
queue snapshots. A key therefore lands in exactly one shard: no overlap, no
gaps. Choose the key so that rows that must be handled together share it
(e.g. all attempts of one case -> key = case_id).
"""
from __future__ import annotations

import math
import os
import zlib


def _int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


SHARD_COUNT = max(1, _int("SHARD_COUNT", 1))
SHARD_INDEX = _int("SHARD_INDEX", 0)

if not 0 <= SHARD_INDEX < SHARD_COUNT:
    raise SystemExit(f"SHARD_INDEX={SHARD_INDEX} is outside 0..{SHARD_COUNT - 1}")


def enabled() -> bool:
    return SHARD_COUNT > 1


def in_shard(key) -> bool:
    """True when `key` belongs to this shard (always True when sharding is off)."""
    if not enabled():
        return True
    return zlib.crc32(str(key).strip().encode("utf-8")) % SHARD_COUNT == SHARD_INDEX


def share_of(cap: int) -> int:
    """A per-run budget (e.g. 'max single lookups') split evenly across shards,
    so N shards together spend about the original budget, not N times it."""
    if not enabled():
        return cap
    return max(1, math.ceil(cap / SHARD_COUNT))


def describe() -> str:
    return f"shard {SHARD_INDEX + 1}/{SHARD_COUNT}" if enabled() else "unsharded"
