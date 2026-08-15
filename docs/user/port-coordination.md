---
title: Global port coordination
description: Learn how splashdown allocates sticky, collision-free ports across every checkout.
---

# Global port coordination

The registry at `~/.local/state/splashdown/{ports.tsv,kv.tsv}` is **machine-wide**, not per-repo. When any checkout allocates a port, the allocator considers:

1. Every other checkout's pinned ports (any repo, any worktree)
2. Live `bind()` probes (catches ports held by non-splashdown processes)

So three unrelated projects can each declare `range = [3001, 3100]` and never collide. Splashdown hands them 3001, 3002, 3003 (or whatever's free at allocation time).

Lazy GC: entries for checkouts whose directory no longer exists are dropped on next allocation. That's how `git worktree remove` cleanup works without a hook.
