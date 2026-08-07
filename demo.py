#!/usr/bin/env python3
"""CIB Detector demo: generate synthetic "aged content" then test re-upload detection.

Simulates the exact pattern from the BBC Indonesia story:
  1. Content uploaded months ago (low engagement, dormant).
  2. Same content re-shared recently with a coordinated engagement spike.

Uses ffmpeg to synthesize a talking-head-style clip (testsrc + sine audio),
re-encodes it to simulate a re-upload, and ingests both into the fingerprint store.

Usage:
  python3 demo.py              # full demo
  python3 demo.py --no-video   # skip video fingerprint (audio only)
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fingerprint import FingerprintStore, DB_PATH


def make_clip(path: Path, duration: int = 8, seed: int = 42, audio_tone: int = 440) -> None:
    """Synthesize a deterministic video+audio clip (testsrc2 + sine wave)."""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=duration={duration}:size=640x360:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency={audio_tone}:duration={duration}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True, capture_output=True, timeout=60,
    )


def reencode(path: Path, out: Path, crf: int = 30) -> None:
    """Re-encode a clip to simulate a different upload of the same content."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
         "-c:a", "aac", "-b:a", "96k", str(out)],
        check=True, capture_output=True, timeout=60,
    )


def demo(no_video: bool = False) -> None:
    print("=" * 70)
    print("CIB DETECTOR DEMO — Layer 1 (fingerprint) + Layer 2 (resurrection)")
    print("=" * 70)

    if os.path.exists(DB_PATH):
        os.unlink(DB_PATH)
    store = FingerprintStore()
    print(f"[db] fresh store at {DB_PATH}")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        orig = td / "original.mp4"
        copy_a = td / "reshare_a.mp4"
        copy_b = td / "reshare_b.mp4"
        copy_c = td / "different.mp4"

        print("\n[1] Generating original clip (8s, testsrc+440Hz)...")
        make_clip(orig)
        print(f"    size={orig.stat().st_size} bytes")

        print("\n[2] Simulating upload history — original seen 90 days ago, low engagement...")
        old = (datetime.utcnow() - timedelta(days=90)).isoformat(sep=" ", timespec="seconds")
        r1 = store.ingest(orig, source="original", engagement=10, seen_at=old, url="v1")
        print(f"    → {r1}")

        print("\n[3] Simulating dormant period (no appearances for 85 days)...")

        print("\n[4] Coordinated re-share TODAY: 3 re-encodes of same clip, high engagement spike...")
        for i, crf in enumerate([26, 28, 32]):
            out = td / f"reshare_{i}.mp4"
            reencode(orig, out, crf)
            r = store.ingest(out, source=f"coordinated_{i}", engagement=random.randint(500, 1500),
                             url=f"tiktok://reshare{i}")
            print(f"    re-encode #{i} (crf={crf}) → {r}")

        print("\n[5] Control: a genuinely different clip (smptebars + 880Hz — should NOT match)...")
        other = td / "other.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "smptebars=duration=8:size=640x360:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=880:duration=8",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-shortest", str(other)],
            check=True, capture_output=True, timeout=60,
        )
        r_other = store.ingest(other, source="control", engagement=50, url="v2")
        print(f"    → {r_other}")

        print("\n[6] Layer 2: detect resurrections...")
        flags = store.detect_resurrections(min_age_days=30, spike_ratio=3.0)
        if flags:
            for f in flags:
                print(f"    ⚠ FLAG: content#{f['content_id']} age={f['age_days']}d "
                      f"spike_ratio={f['spike_ratio']} score={f['score']} uploads={f['upload_count']}")
        else:
            print("    (no flags)")

        print("\n[7] DB stats:")
        print(f"    {store.stats()}")

        print("\n" + "=" * 70)
        print("VERDICT")
        print("=" * 70)
        reuploads = store.conn.execute("SELECT id, upload_count FROM content WHERE upload_count>1").fetchall()
        if reuploads:
            print(f"  ✅ Re-upload detected: {len(reuploads)} content id(s) with upload_count>1")
        else:
            print("  ❌ No re-upload found — fingerprint failed")
        flags = store.conn.execute("SELECT COUNT(*) FROM engagement_events").fetchone()[0]
        print(f"  ✅ Resurrection events: {flags}")
        print("\nNote: real pipeline would join these into a CIB score with Layer 3/4 (graph, NLP).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-video", action="store_true", help="skip video fingerprint")
    args = ap.parse_args()
    demo(no_video=args.no_video)