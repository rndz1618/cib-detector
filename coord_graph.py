# CIB Detector - Layer 3: Coordinated Graph Detection
"""
Detects *coordinated* amplification: a set of accounts that repeatedly
amplify the same content within short time windows AND are connected in a
social-membership graph. This is the signature that distinguishes a botnet /
coordinated campaign from organic virality.

Signals computed per cluster:
  - temporal_coordination : share events clustered in a tight time window
  - account_freshness      : mixture of young (sockpuppet) accounts
  - graph_connectivity     : how densely the accounts share membership edges
  - content_concentration  : how much of the cluster's output targets ONE asset

A cluster scores high when several co-occur — the "mustahil dilakukan manual"
pattern from the BBC Indonesia case.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import json
import sqlite3

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "graph.db"


def cluster_indices(times: list[datetime], window_minutes: float) -> list[list[int]]:
    """Greedy single-linkage time clustering. Returns lists of indices."""
    order = sorted(range(len(times)), key=lambda i: times[i])
    clusters: list[list[int]] = []
    for idx in order:
        placed = False
        for cl in clusters:
            if (times[idx] - times[cl[-1]]).total_seconds() <= window_minutes * 60:
                cl.append(idx)
                placed = True
                break
        if not placed:
            clusters.append([idx])
    return clusters


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    follower_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS follows (
    follower_id INTEGER NOT NULL REFERENCES accounts(id),
    followee_id INTEGER NOT NULL REFERENCES accounts(id),
    PRIMARY KEY (follower_id, followee_id)
);

CREATE TABLE IF NOT EXISTS shares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    content_key TEXT NOT NULL,
    shared_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shares_content ON shares(content_key);
CREATE INDEX IF NOT EXISTS idx_shares_time ON shares(shared_at);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_key TEXT NOT NULL,
    size INTEGER NOT NULL,
    cohesion REAL NOT NULL,
    avg_age_days REAL NOT NULL,
    connectivity REAL NOT NULL,
    concentration REAL NOT NULL,
    score REAL NOT NULL,
    detected_at TEXT NOT NULL,
    members TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.utcnow().isoformat(sep=" ", timespec="seconds")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace(" ", "T"))


@dataclass
class ClusterFlag:
    content_key: str
    size: int
    cohesion: float
    avg_age_days: float
    connectivity: float
    concentration: float
    score: float
    members: list[str]


class CoordGraph:
    def __init__(self, db: Path = DB_PATH):
        self.conn = sqlite3.connect(str(db), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------- helpers

    def add_account(self, username: str, created_at: str,
                    platform: str = "", followers: int = 0) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO accounts(username, created_at, platform, follower_count) "
            "VALUES(?,?,?,?)", (username, created_at, platform, followers))
        self.conn.execute(
            "UPDATE accounts SET created_at=?, platform=?, follower_count=? WHERE username=?",
            (created_at, platform, followers, username))
        return self._id(username)

    def _id(self, username: str) -> int | None:
        r = self.conn.execute("SELECT id FROM accounts WHERE username=?",
                              (username,)).fetchone()
        return r["id"] if r else None

    def add_follow(self, follower: str, followee: str) -> None:
        f, t = self._id(follower), self._id(followee)
        if f and t and f != t:
            self.conn.execute(
                "INSERT OR IGNORE INTO follows(follower_id, followee_id) VALUES(?,?)",
                (f, t))

    def add_share(self, username: str, content_key: str, shared_at: str) -> None:
        aid = self._id(username)
        if aid:
            self.conn.execute(
                "INSERT INTO shares(account_id, content_key, shared_at) VALUES(?,?,?)",
                (aid, content_key, shared_at))

    # --------------------------------------------------------------- graph

    def _adjacency(self) -> dict[int, set[int]]:
        adj: dict[int, set[int]] = defaultdict(set)
        for r in self.conn.execute("SELECT follower_id, followee_id FROM follows"):
            adj[r["follower_id"]].add(r["followee_id"])
            adj[r["followee_id"]].add(r["follower_id"])
        return adj

    def _components(self, adj: dict[int, set[int]], nodes: set[int]) -> int:
        seen: set[int] = set()
        comps = 0
        for n in nodes:
            if n in seen:
                continue
            comps += 1
            q = deque([n])
            seen.add(n)
            while q:
                c = q.popleft()
                for nb in adj.get(c, set()):
                    if nb in nodes and nb not in seen:
                        seen.add(nb)
                        q.append(nb)
        return comps

    def _collect_account_ages(self) -> dict[int, int]:
        now = datetime.utcnow()
        out: dict[int, int] = {}
        for r in self.conn.execute("SELECT id, created_at FROM accounts"):
            try:
                out[r["id"]] = max(0, (now - _parse(r["created_at"])).days)
            except Exception:
                out[r["id"]] = 0
        return out

    # -------------------------------------------------------- detection

    def detect_coordinated(self, window_minutes: float = 60, min_cluster: int = 3,
                           sockpuppet_age_days: int = 90) -> list[ClusterFlag]:
        now = datetime.utcnow()
        adj = self._adjacency()
        ages = self._collect_account_ages()

        by_content: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
        for r in self.conn.execute(
                "SELECT account_id, content_key, shared_at FROM shares"):
            by_content[r["content_key"]].append(
                (r["account_id"], _parse(r["shared_at"])))

        flags: list[ClusterFlag] = []
        for content_key, events in by_content.items():
            events.sort(key=lambda x: x[1])
            times = [t for _, t in events]
            for indices in cluster_indices(times, window_minutes):
                if len(indices) < min_cluster:
                    continue
                members = set(events[i][0] for i in indices)
                if len(members) < min_cluster:
                    continue

                # temporal cohesion: tighter spread = higher
                span_hrs = (max(times[i] for i in indices) -
                            min(times[i] for i in indices)).total_seconds() / 3600.0
                cohesion = 1.0 / (1.0 + span_hrs)

                # freshness: fraction of young (sockpuppet) accounts
                member_ages = [ages.get(m, sockpuppet_age_days) for m in members]
                young = sum(1 for a in member_ages if a < sockpuppet_age_days)
                freshness = young / len(members) if members else 0.0
                avg_age = sum(member_ages) / len(member_ages) if member_ages else 0.0

                # connectivity: 1 = one connected component, 0 = full isolate
                comps = self._components(adj, members)
                connectivity = (1.0 / comps) if comps else 0.0

                # concentration: how much of members' share output targets THIS content
                this_total = 0
                member_share_total = 0
                for m in members:
                    rows = self.conn.execute(
                        "SELECT content_key FROM shares WHERE account_id=?", (m,)).fetchall()
                    member_share_total += len(rows)
                    this_total += sum(1 for rr in rows if rr["content_key"] == content_key)
                concentration = (this_total / member_share_total) if member_share_total else 0.0

                # weighted score — the "coordinated" joint signature
                score = (0.35 * cohesion + 0.25 * freshness +
                         0.25 * connectivity + 0.15 * concentration)

                usernames = [
                    self.conn.execute("SELECT username FROM accounts WHERE id=?",
                                      (m,)).fetchone()["username"]
                    for m in members
                ]
                flags.append(ClusterFlag(
                    content_key=content_key,
                    size=len(members),
                    cohesion=round(cohesion, 3),
                    avg_age_days=round(avg_age, 1),
                    connectivity=round(connectivity, 3),
                    concentration=round(concentration, 3),
                    score=round(score, 2),
                    members=usernames,
                ))

        flags.sort(key=lambda f: f.score, reverse=True)
        return flags

    def persist_flags(self, flags: list[ClusterFlag]) -> None:
        for f in flags:
            self.conn.execute(
                "INSERT INTO clusters(content_key, size, cohesion, avg_age_days, "
                "connectivity, concentration, score, detected_at, members) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (f.content_key, f.size, f.cohesion, f.avg_age_days,
                 f.connectivity, f.concentration, f.score, _now(),
                 json.dumps(f.members)))
        self.conn.commit()


if __name__ == "__main__":
    print("CoordGraph Layer 3 module ready.")