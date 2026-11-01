# Z3 #11500: A/B five-pair comparison

A: `953e43301f406bbf41cf1ae8e600641345a8823b` (PR #11505, selected for our fork main).
B: `ae64857b47ed6123c034c825e4b4dcb930e75b81` (PR #11506, alternative).
Both derive from `8fbb275aba0472e862e289c7af1331b3bd5d512c`.

Linux, Python 3, GNU time, taskset, CMake and a C++ compiler are required. Build both revisions with the same compiler and Release settings:

```sh
git clone https://github.com/msuadOf/z3.git z3-a
git -C z3-a checkout 953e43301f406bbf41cf1ae8e600641345a8823b
cmake -S z3-a -B z3-a/build -DCMAKE_BUILD_TYPE=Release
cmake --build z3-a/build --target shell -j 24

git clone https://github.com/msuadOf/z3.git z3-b
git -C z3-b checkout ae64857b47ed6123c034c825e4b4dcb930e75b81
cmake -S z3-b -B z3-b/build -DCMAKE_BUILD_TYPE=Release
cmake --build z3-b/build --target shell -j 24
```

From this material directory, run the suites (output directories must not already exist):

```sh
python3 reproduce.py --a /absolute/path/z3-a/build/z3 --b /absolute/path/z3-b/build/z3 \
  --suite mixed-five --cpus 33 47 49 52 56 60 37 17 --output rerun-mixed
python3 reproduce.py --a /absolute/path/z3-a/build/z3 --b /absolute/path/z3-b/build/z3 \
  --suite enum-six-five --cpus 33 47 49 52 --output rerun-enum
```

Replace CPU IDs with available logical CPUs on distinct physical cores (`lscpu -p=CPU,CORE,SOCKET`). Each workload always uses its assigned CPU for both A and B; separate workloads run concurrently. The published CPU order is the insertion order in each suite's manifest. Avoid occupying SMT siblings. No CPU exclusivity is claimed on our shared machine.

Each workload runs five pairs: AB, BA, AB, BA, AB; fresh processes, no extra warmup, default solver seeds/threads, explicit `-t:7200000` (milliseconds), 7210-second process cap. Wall time includes startup, parsing and solving. The timer blocks on process termination; an independent watchdog only enforces the process cap. All 120 original runs returned SAT with exit 0. This measures individual SMT queries, not complete Isla Clauses.

For a single query:

```sh
/usr/bin/time -v taskset -c 33 /absolute/path/z3-a/build/z3 -t:7200000 -in < mixed-five/flags-dump-11/query.smt2
```

## Workload contents

Exact queries are included, with SHA256 in `query-sha256.json`; results and all five per-version observations are in each suite's `report.md`, `summary.json`, and per-run folders. `manifest.json` records original source revisions, binary hashes, input hashes, CPU mapping and environment. Historical command paths are archival, not runnable entry points; use `reproduce.py` above.

- `mixed-five`: real NaN and three real flags queries from Isla symbolic execution, a minimal reproducer, and synthetic enum sizes 6/512/1024 × 256 variables. Large constructor counts are stress boundaries, not typical Sail enum sizes.
- `flags-dump-11`: a path in the single-precision arithmetic Clause `F_BIN_RM_TYPE_S`, constrained to FDIV.S with round-toward-zero. It asks whether `old_fflags != (old_fflags | new_flags)` is reachable. The exact query includes symbolic register selection, NaN-boxing/path constraints, f32 division and f64 upward/downward divisions used in exception-flag computation. It is a complete exported query, not a complete instruction execution campaign. Its original export is `zF_BIN_RM_TYPE_S-pid3379837-CheckSatAssuming-event8.smt2`; `11` is a local input index, not an instruction ID.
- `enum-six-five`: 6 constructors and 64/256/512/1024 free enum variables. Each variable has the independent constraint `e_i != C0`; the FP constraint is independently `fp.isNaN(x)`. This tests variable-count overhead without realistic FP/enum coupling. The 256-variable file is byte-identical to the mixed-suite 6×256 file.

To generate the small-enum content directly:

```python
for count in [64, 256, 512, 1024]:
    lines = ["(declare-datatypes () ((E C0 C1 C2 C3 C4 C5)))",
             "(declare-const x (_ FloatingPoint 8 24))",
             "(assert (fp.isNaN x))"]
    for i in range(count):
        lines += [f"(declare-const e{i} E)", f"(assert (not (= e{i} C0)))"]
    lines += ["(check-sat-using default)"]
    open(f"enum-6-{count}.smt2", "w").write("\n".join(lines) + "\n")
```

Original host: AMD EPYC 7543, 2 sockets × 32 cores, SMT2; GCC 15.2.0; CMake 4.2.3; Release `-O3 -DNDEBUG`, no LTO/GMP. Build self-reports Z3 5.3.4. See `machine.txt`.

A was chosen for our current real floating-point slow queries. B is faster on the synthetic enum cases. Neither result establishes universal superiority or unchanged timeouts under every resource budget. Short-query startup/shared-host effects and B's real-query timing spread are visible in the raw results. Do not pool timings across the two separately scheduled suites or treat their 6×256 difference as a source-code change.
