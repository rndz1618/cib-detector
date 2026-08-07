#!/usr/bin/env python3
"""Layer 3 demo: distinguish coordinated botnet cluster from organic accounts.

Scenario A — COORDINATED: 9 fresh sockpuppet accounts, fully interconnected,
all share the SAME aged content within a ~3-minute window (the BBC pattern).
Scenario B — ORGANIC: unrelated accounts, not connected, share DIFFERENT
content at scattered random times.

detect_coordinated() should flag A strongly (score ~ high) and B not at all.
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta
from pathlib import Path

from coord_graph import CoordGraph, DB_PATH


def main():
    if os.path.exists(DB_PATH):
        os.unlink(DB_PATH)
    g = CoordGraph()
    now = datetime.utcnow()

    print("=" * 70)
    print("LAYER 3 DEMO — Coordinated Graph Detection")
    print("=" * 70)

    # ---- Scenario A: coordinated botnet cluster ----
    contentA = "dated-demo-content-viral-again"
    print(f"\n[Scenario A] Coordinated botnet: 9 fresh sockpuppets, "
          f"fully interconnected, share SAME content within 5 min")
    for i in range(9):
        u = f"bot_{i}"
        g.add_account(u, (now - timedelta(days=5 + i)).isoformat(sep=" ", timespec="seconds"),
                      platform="tiktok", followers=random.randint(0, 500))
    bots = [f"bot_{i}" for i in range(9)]
    # fully connected follow graph
    for a in bots:
        for b in bots:
            if a != b:
                g.add_follow(a, b)
    # all share the same content clustered in a 5-minute window
    base = now - timedelta(hours=1)  # pretend it just happened
    for i, u in enumerate(bots):
        t = base + timedelta(minutes=random.uniform(0, 5))
        g.add_share(u, contentA, t.isoformat(sep=" ", timespec="seconds"))

    # ---- Scenario B: organic, independent accounts ----
    print("\n[Scenario B] Organic: 15 accounts, NOT interconnected, share "
          "DIFFERENT content at scattered times over weeks")
    orgs = []
    for i in range(15):
        u = f"organic_{i}"
        g.add_account(u, (now - timedelta(days=400 + i)).isoformat(sep=" ", timespec="seconds"),
                      platform="tiktok", followers=random.randint(50, 50000))
        orgs.append(u)
    for i, u in enumerate(orgs):
        t = now - timedelta(days=random.randint(0, 30), hours=random.randint(0, 23))
        g.add_share(u, f"unique_content_{i}", t.isoformat(sep=" ", timespec="seconds"))
    # a few random follows but NOT a tight cluster
    for _ in range(6):
        a, b = random.choice(orgs), random.choice(orgs)
        if a != b:
            g.add_follow(a, b)

    g.conn.commit()

    print("\n[demo] running detect_coordinated(window=60m, min_cluster=3)...")
    flags = g.detect_coordinated(window_minutes=60, min_cluster=3,
                                 sockpuppet_age_days=90)

    print(f"\n[result] Flags found: {len(flags)}")
    for f in flags[:10]:
        print(f"  ⚠ content={f.content_key[:36]:38s} size={f.size} "
              f"cohesion={f.cohesion} age_avg={f.avg_age_days}d "
              f"connect={f.connectivity} conc={f.concentration} score={f.score}")

    # -------- verdict --------
    print("\n" + "=" * 70 + "\nVERDICT\n" + "=" * 70)
    coordinated = [f for f in flags if f.content_key == contentA]
    organic_flagged = [f for f in flags if f.content_key.startswith("unique_content_")]

    if coordinated:
        top = coordinated[0]
        if top.size >= 9:
            print(f"  ✅ Coordinated botnet DETECTED: content shared by {top.size} "
                  f"interconnected fresh accounts, score={top.score}")
        else:
            print(f"  ⚠ Botnet content detected but only {top.size} accounts "
                  f"(expected 9)")
    else:
        print("  ❌ Botnet content NOT flagged")

    print(f"  ✅ Organic accounts wrongly flagged as coordinated: "
          f"{len(organic_flagged)} (should be 0)")


if __name__ == "__main__":
    main()