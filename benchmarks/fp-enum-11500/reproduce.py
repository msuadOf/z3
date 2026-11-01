from pathlib import Path
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import signal
import statistics
import subprocess
import threading
import time

import argparse

parser = argparse.ArgumentParser(description="Reproduce the five-pair FP/enum comparison")
parser.add_argument("--a", required=True, type=Path, help="A z3 executable")
parser.add_argument("--b", required=True, type=Path, help="B z3 executable")
parser.add_argument("--suite", choices=["mixed-five", "enum-six-five"], required=True)
parser.add_argument("--cpus", type=int, nargs="+", required=True, help="One logical CPU per workload, in published manifest order")
parser.add_argument("--output", type=Path, required=True, help="A new, empty output directory")
ARGS = parser.parse_args()
INPUT = Path(__file__).resolve().parent / ARGS.suite
OUT = ARGS.output.resolve()
OUT.mkdir(parents=True, exist_ok=False)
LOCK = threading.Lock()
ROWS = []
CASES = {}
CPUS = ARGS.cpus
BINARIES = {"A": ARGS.a.resolve(), "B": ARGS.b.resolve()}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(path)


def save_report():
    summaries = {}
    lines = ["# 发布版 A/B：相同 workload 各五次交叉实验", "",
             "顺序 AB/BA/AB/BA/AB；同一 workload 同 CPU。每次 SMT 7200 秒、外限 7210 秒。",
             "A=953e433，B=ae64857。独立 query 实验，机器共享；不是完整 Clause。", "",
             f"已完成 {len(ROWS)}/{len(CASES)*10} 次。更新时间（UTC）：{now()}", "",
             "| workload | CPU | A 已完成 | B 已完成 | A 中位秒 | B 中位秒 | B/A 中位耗时比 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, case in CASES.items():
        stats = {}
        for variant in BINARIES:
            rs = sorted([r for r in ROWS if r["case"] == name and r["variant"] == variant], key=lambda r: r["round"])
            values = [r["wall_s"] for r in rs if r["pass"]]
            stats[variant] = {"completed": len(rs), "passed": len(values),
                              "wall_s": [r["wall_s"] for r in rs],
                              "statuses": [r["statuses"] for r in rs],
                              "median_s": statistics.median(values) if values else None,
                              "mean_s": statistics.mean(values) if values else None,
                              "range_s": [min(values), max(values)] if values else None}
        a, b = stats["A"], stats["B"]
        complete = a["passed"] == b["passed"] == 5
        ratio = b["median_s"] / a["median_s"] if complete else None
        stats["complete"] = complete
        stats["B_over_A_median_ratio"] = ratio
        summaries[name] = stats
        fmt = lambda x: "—" if x is None else f"{x:.4f}"
        lines.append(f"| {name} | {case['cpu']} | {a['completed']}/5 | {b['completed']}/5 | {fmt(a['median_s'])} | {fmt(b['median_s'])} | {fmt(ratio)} |")
    lines += ["", "## 逐次墙钟耗时（秒）", "",
              "| workload | 版本 | 第1次 | 第2次 | 第3次 | 第4次 | 第5次 |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, stats in summaries.items():
        for v in BINARIES:
            vals = [f"{x:.4f}" for x in stats[v]["wall_s"]]
            lines.append("| " + " | ".join([name, v] + vals + ["—"] * (5-len(vals))) + " |")
    lines += ["", "失败/UNKNOWN/外限不会当作成功求解计入中位数；全部完成且各臂五次SAT才给耗时比。",
              "原始命令、stdout/stderr、GNU time、负载及起止时间见各 workload/round-*/。"]
    write_json(OUT / "summary.json", {"updated_utc": now(), "completed": len(ROWS), "total": len(CASES)*10,
                                      "all_pass": len(ROWS) == len(CASES)*10 and all(r["pass"] for r in ROWS),
                                      "cases": summaries, "runs": ROWS})
    temp = OUT / "report.md.tmp"
    temp.write_text("\n".join(lines) + "\n")
    temp.replace(OUT / "report.md")


def run_case(item):
    name, case = item
    for rep in range(5):
        for v in (["A", "B"] if rep % 2 == 0 else ["B", "A"]):
            folder = OUT / name / f"round-{rep+1}-{v}"
            folder.mkdir()
            timing = folder / "time.json"
            cmd = ["/usr/bin/time", "-f", '{"user_s":%U,"sys_s":%S,"max_rss_kib":%M}', "-o", str(timing),
                   "taskset", "-c", str(case["cpu"]), str(BINARIES[v]), "-t:7200000", "-in"]
            row = {"case": name, "variant": v, "round": rep+1, "cpu": case["cpu"],
                   "command": cmd, "started_utc": now(), "load_start": os.getloadavg(),
                   "query_sha256": case["sha256"]}
            start = time.monotonic()
            with (OUT / name / "query.smt2").open("rb") as inp, (folder / "stdout").open("wb") as stdout, (folder / "stderr").open("wb") as stderr:
                proc = subprocess.Popen(cmd, stdin=inp, stdout=stdout, stderr=stderr, start_new_session=True)
                write_json(folder / "running.json", {**row, "pid": proc.pid})
                expired = threading.Event()
                def kill_after_budget():
                    expired.set()
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                timer = threading.Timer(7210, kill_after_budget)
                timer.daemon = True
                timer.start()
                code = proc.wait()
                timer.cancel()
                outer_timeout = expired.is_set()
                if outer_timeout:
                    code = 124
            row.update(wall_s=time.monotonic()-start, exit=code, external_timeout=outer_timeout,
                       ended_utc=now(), load_end=os.getloadavg())
            output = (folder / "stdout").read_text()
            row["statuses"] = re.findall(r"^(sat|unsat|unknown)$", output, re.M)
            row["pass"] = code == 0 and row["statuses"] == ["sat"] and "(error" not in output
            if timing.exists():
                try:
                    row.update(json.loads(timing.read_text()))
                except json.JSONDecodeError:
                    row["time_raw"] = timing.read_text()
            write_json(folder / "result.json", row)
            (folder / "running.json").unlink()
            with LOCK:
                ROWS.append(row)
                save_report()
                print(f"{name} round={rep+1} {v} {row['wall_s']:.4f}s {row['statuses']} pass={row['pass']}", flush=True)


def main():
    published = json.loads((INPUT / "manifest.json").read_text())
    sources = {name: (INPUT / name / "query.smt2").read_text() for name in published["cases"]}
    assert len(CPUS) == len(sources), f"Supply {len(sources)} CPUs"
    assert len(set(CPUS)) == len(CPUS), "Workload CPUs must differ"
    assert set(CPUS).issubset(os.sched_getaffinity(0)), "CPU outside current affinity"
    physical = []
    for cpu in CPUS:
        topo = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        physical.append(((topo / "physical_package_id").read_text().strip(),
                         (topo / "core_id").read_text().strip()))
    assert len(set(physical)) == len(CPUS), "Choose distinct physical cores"
    manifests = {}
    for v, binary in BINARIES.items():
        assert binary.is_file()
        manifests[v] = {"source_commit": published["variants"][v]["commit"],
                        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                        "version": subprocess.check_output([str(binary), "--version"], text=True).strip()}
    for (name, source), cpu in zip(sources.items(), CPUS):
        assert source.count("(check-sat-using default)") == 1
        assert not re.search(r"\(set-option\s+:(timeout|rlimit)", source)
        folder = OUT / name
        folder.mkdir()
        query = source.encode()
        (folder / "query.smt2").write_bytes(query)
        CASES[name] = {"cpu": cpu, "sha256": hashlib.sha256(query).hexdigest()}
    write_json(OUT / "manifest.json", {"started_utc": now(), "pid": os.getpid(), "variants": manifests,
                                       "cases": CASES, "order": ["AB", "BA", "AB", "BA", "AB"],
                                       "smt_budget_ms": 7200000, "outer_budget_s": 7210, "warmup_runs": 0,
                                       "cpu_topology": subprocess.check_output(["lscpu", "-p=CPU,CORE,SOCKET,ONLINE"], text=True),
                                       "load_start": os.getloadavg()})
    save_report()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(CASES)) as pool:
        list(pool.map(run_case, CASES.items()))
    assert all(r["pass"] for r in ROWS), "One or more runs failed; see report.md"
    print(f"Completed {len(ROWS)}/{len(CASES)*10} runs; all SAT. See report.md and summary.json.", flush=True)


if __name__ == "__main__":
    main()
