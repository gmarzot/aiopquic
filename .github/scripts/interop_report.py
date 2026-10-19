"""Interop run report for the GitHub job summary: one matrix per protocol and role,
outcome pies, and aiopquic's QUIC score next to aioquic's and picoquic's from the
newest public Interop Runner run.

    python interop_report.py RESULTS_DIR [--public-base URL]

RESULTS_DIR holds the runner's per-pair JSON files (-j output). Exit status is
1 when it holds none.
"""

import argparse
import glob
import json
import os
import statistics
import sys
import urllib.request

ME = "aiopquic"
MARK = {"succeeded": "✅", "failed": "❌", "unsupported": "➖"}
COMPETITORS = ("aioquic", "picoquic")


def load_pairs(results_dir):
    """{protocol: {(server, client): {"tests": [...], "measurements": [...], "names": {...}}}}"""
    pairs = {}
    for path in sorted(glob.glob(os.path.join(results_dir, "**", "*.json"), recursive=True)):
        try:
            data = json.load(open(path))
        except (OSError, ValueError):
            continue
        if not data.get("servers") or not data.get("clients") or not data.get("results"):
            continue
        protocol = "webtransport" if "webtransport" in path else "quic"
        names = {abbr: t.get("name", abbr) for abbr, t in (data.get("tests") or {}).items()}
        servers, clients = data["servers"], data["clients"]
        for ci, client in enumerate(clients):
            for si, server in enumerate(servers):
                idx = ci * len(servers) + si
                meas = data["measurements"][idx] if idx < len(data.get("measurements") or []) else []
                pairs.setdefault(protocol, {})[(server, client)] = {
                    "tests": data["results"][idx], "measurements": meas or [], "names": names}
    return pairs


def passed(tests):
    return sum(1 for t in tests if t["result"] == "succeeded")


def matrix(pairs, role):
    """Rows: one peer per pair with aiopquic in `role`; columns: test abbreviations."""
    rows = [(c if role == "server" else s, p) for (s, c), p in pairs.items()
            if (s if role == "server" else c) == ME]
    if not rows:
        return []
    rows.sort(key=lambda r: (r[0] != ME, r[0]))
    cols = []
    for _, p in rows:
        for t in p["tests"]:
            if t["abbr"] not in cols:
                cols.append(t["abbr"])
    peer = "client" if role == "server" else "server"
    out = [f"| {peer} | " + " | ".join(cols) + " | passed |",
           "|---|" + "---|" * len(cols) + "---|"]
    for name, p in rows:
        by = {t["abbr"]: t["result"] for t in p["tests"]}
        cells = [MARK.get(by.get(c), "⬜") for c in cols]
        out.append(f"| {name} | " + " | ".join(cells) + f" | {passed(p['tests'])}/{len(p['tests'])} |")
    return out


def legend(pairs):
    names = {}
    for p in pairs.values():
        names.update(p["names"])
        for t in p["tests"]:
            names.setdefault(t["abbr"], t["name"])
    items = ", ".join(f"`{a}` {n}" for a, n in names.items())
    return ["<details><summary>Test cases</summary>", "", items, "", "</details>"]


def pie(title, counts):
    lines = ["```mermaid", f"pie title {title}"]
    for label, n in counts.items():
        if n:
            lines.append(f'    "{label}" : {n}')
    return lines + ["```"]


def outcome_counts(pairs):
    counts = {"passed": 0, "failed": 0, "unsupported": 0}
    for p in pairs.values():
        for t in p["tests"]:
            key = {"succeeded": "passed", "failed": "failed"}.get(t["result"], "unsupported")
            counts[key] += 1
    return counts


def public_scores(base, peers):
    """Mean cases passed per pair for each competitor, as server and as client, against `peers`."""
    runs = json.load(urllib.request.urlopen(f"{base}/logs.json", timeout=20))
    run = runs[-1]
    data = json.load(urllib.request.urlopen(f"{base}/{run}/result.json", timeout=20))
    servers, clients = data["servers"], data["clients"]

    def score(server, client):
        if server not in servers or client not in clients:
            return None
        return passed(data["results"][clients.index(client) * len(servers) + servers.index(server)])

    out = {}
    for stack in COMPETITORS:
        srv = [x for x in (score(stack, c) for c in peers) if x is not None]
        cli = [x for x in (score(s, stack) for s in peers) if x is not None]
        out[stack] = (statistics.mean(srv) if srv else None, statistics.mean(cli) if cli else None)
    return run, out


def our_scores(pairs, peers):
    srv = [passed(pairs[(ME, c)]["tests"]) for c in peers if (ME, c) in pairs]
    cli = [passed(pairs[(s, ME)]["tests"]) for s in peers if (s, ME) in pairs]
    return (statistics.mean(srv) if srv else None, statistics.mean(cli) if cli else None)


def _fmt(v):
    return "–" if v is None else f"{v:.1f}"


def comparison(quic, base):
    peers = sorted({x for pair in quic for x in pair if x != ME})
    if not peers:
        return []
    total = max(len(p["tests"]) for p in quic.values())
    rows = {ME: our_scores(quic, peers)}
    lines = []
    try:
        run, theirs = public_scores(base, peers)
        rows.update(theirs)
        source = f"public run [{run}]({base.rsplit('/logs', 1)[0]})"
    except Exception as exc:
        source = f"public results unavailable ({exc.__class__.__name__})"
    lines += [f"QUIC cases passed per pair (of {total}) against the same {len(peers)} peers "
              f"({', '.join(peers)}); aiopquic from this run, the others from the {source}.", "",
              "| stack | as server | as client |", "|---|---|---|"]
    lines += [f"| {s} | {_fmt(v[0])} | {_fmt(v[1])} |" for s, v in rows.items()]
    labels, values = [], []
    for s, (srv, cli) in rows.items():
        for role, v in (("server", srv), ("client", cli)):
            if v is not None:
                labels.append(f'"{s} {role}"')
                values.append(f"{v:.1f}")
    if values:
        lines += ["", "```mermaid", "xychart-beta",
                  '    title "QUIC cases passed per pair"',
                  f"    x-axis [{', '.join(labels)}]",
                  f'    y-axis "cases" 0 --> {total}',
                  f"    bar [{', '.join(values)}]", "```"]
    return lines


def measurements(pairs):
    rows = [(s, c, m) for (s, c), p in sorted(pairs.items()) for m in p["measurements"]]
    if not rows:
        return []
    out = ["| server | client | measurement | result | details |", "|---|---|---|---|---|"]
    out += [f"| {s} | {c} | {m['name']} | {MARK.get(m['result'], m['result'])} | {m.get('details', '')} |"
            for s, c, m in rows]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--public-base", default="https://interop.seemann.io/logs/quic")
    args = ap.parse_args()
    pairs = load_pairs(args.results_dir)
    if not pairs:
        print("No Interop Runner results found.")
        return 1
    out = ["## Interop report", ""]
    if "quic" in pairs:
        out += ["### Score"] + [""] + comparison(pairs["quic"], args.public_base) + [""]
    for protocol, title in (("quic", "QUIC (hq-interop)"), ("webtransport", "WebTransport")):
        if protocol not in pairs:
            continue
        p = pairs[protocol]
        out += [f"### {title}", ""] + pie(f"{title}: all cases", outcome_counts(p)) + [""]
        for role in ("server", "client"):
            m = matrix(p, role)
            if m:
                out += [f"**aiopquic as {role}**", ""] + m + [""]
        out += legend(p) + [""]
        meas = measurements(p)
        if meas:
            out += ["<details><summary>Measurements</summary>", ""] + meas + ["", "</details>", ""]
    out.append("✅ passed · ❌ failed · ➖ unsupported · ⬜ not run")
    text = "\n".join(out) + "\n"
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
