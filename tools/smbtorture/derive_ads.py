#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2024-2026 Chimera-NAS Project Contributors
#
# SPDX-License-Identifier: Unlicense
"""Catalog, collection and verdict for tools/smbtorture/derive_ads.sh.

   The sweep (bash) and the verdict (here) meet at one file, results.txt, whose
   rows are `<phase>|<rc>|<gate-hits>|<subtest>`.  Splitting them there is what
   makes the verdict testable without a 655-subtest sweep: tests/test_derive_ads.py
   drives every branch below from fixture rows in milliseconds.

   What a subtest is classified on is whether the FEATURE CHANGES ITS OUTCOME --
   it is run once with named streams forced off and once forced on -- and not on
   whether it tripped the gate.  Those are not the same question, and the
   difference is the whole reason this file exists:

       off    on     meaning                              belongs in the list?
       ----   ----   ----------------------------------   --------------------
       fail   pass   needs the feature                    yes
       fail   fail   needs it to reach its real failure   yes, still failing
       pass   fail   negative test; requires it OFF       NEVER
       pass   pass   indifferent                          no; stale if listed

   The gate-hit criterion this replaced could not tell row 1 from row 3, so it
   reported smb2.create_no_streams.no_stream -- which trips the gate four times
   and passes BECAUSE it does -- as drift, and told the maintainer to add it,
   which turns its PASS into a FAIL with OBJECT_NAME_NOT_FOUND on every backend.
   Nor could it see row 3 or row 4 at all, so a stale or over-broad entry
   survived forever and only the missing direction was ever checked.

   Gate hits are still collected, but as evidence rather than as the criterion:
   they are what proves the sweep observed anything at all (see the floor in
   classify), and they pick the subtests worth a second run.

   Subcommands:
     catalog     `smbtorture --list` output        -> one subtest name per line
     collect     one phase's log tree              -> results rows
     candidates  phase-off rows                    -> subtests worth running on
     classify    results.txt + the checked-in list -> verdict, exit 0/1/2
     names       expectations file                 -> the subtest names it pins
     check       results.txt + expected answers    -> exit 0/1, for the smoke run
"""

import argparse, os, re, sys

# What the driver itself said about the run, harvested from its own output
# rather than inferred from the exit status.  A denylist of bad statuses is the
# same shape as the strcmp chain this whole mechanism replaced, and it does not
# work here: smb2.getinfo.complex reaches its assertion, reports FAILED, and
# THEN aborts at teardown on a leaked evpl buffer, so the process exits 134 --
# a crash status for a run that reached a perfectly good verdict.  Requiring a
# positive token instead means a run that died before saying anything is
# inconclusive no matter which signal killed it.
OUT_PASS      = "pass"
OUT_FAIL      = "fail"
OUT_NOSTART   = "nostart"      # never printed "Server started"
OUT_NOVERDICT = "noverdict"    # started, then died before reporting

CONCLUSIVE = (OUT_PASS, OUT_FAIL)

# The driver's markers.  Changing either string in smbtorture_test.c without
# changing these makes every run inconclusive, which is loud -- the failure mode
# this replaced was silent.
MARK_STARTED = "Server started"
MARK_VERDICT = re.compile(r"^smbtorture: (PASSED|FAILED)", re.M)

# rc column stand-ins for a run that left no usable log.  Distinct because they
# fail differently: nolog means the sweep never dispatched the subtest at all,
# nomarker means the runner wrote a log but not its exit status.
RC_NOLOG    = "nolog"
RC_NOMARKER = "nomarker"

PHASE_OFF = "off"
PHASE_ON  = "on"

CLASS_NEEDS       = "needs-ads"
CLASS_NEEDS_FAIL  = "needs-ads-still-failing"
CLASS_NEGATIVE    = "negative"
CLASS_INDIFFERENT = "indifferent"

# The two classes the checked-in list is supposed to carry.
LISTED_CLASSES = (CLASS_NEEDS, CLASS_NEEDS_FAIL)

CLASS_HEADING = {
    CLASS_NEEDS:       "needs ADS",
    CLASS_NEEDS_FAIL:  "needs ADS, still fails once enabled",
    CLASS_NEGATIVE:    "must stay OFF -- enabling the feature breaks them",
    CLASS_INDIFFERENT: "indifferent",
}

_MANGLE = re.compile(r"[^A-Za-z0-9]")


def mangle(name):
    """Log-file stem for a subtest name.  Mirrors the runner in derive_ads.sh."""
    return _MANGLE.sub("_", name)


def covered(name, patterns):
    """Does the checked-in list cover `name`?

    Exact, or as a parent-suite prefix: "smb2.streams" covers "smb2.streams.io"
    but not "smb2.stream-inherit-perms".  Kept identical to ads_entry_matches()
    in smbtorture_test.c and _smbtorture_needs_ads() in CMakeLists.txt.
    """
    return any(name == p or name.startswith(p + ".") for p in patterns)


def read_patterns(path):
    """The checked-in list, stripped of comments and blanks."""
    patterns = []
    with open(path) as fp:
        for line in fp:
            line = line.split("#", 1)[0].strip()
            if line:
                patterns.append(line)
    return patterns


def read_catalog(stream):
    """`smbtorture --list` output -> sorted, de-duplicated subtest names.

    Every line repeats the subtest's display name as a trailing dotted component
    ("smb2.getinfo.complex.complex"), space-bearing names included
    ("smb2.ioctl.copy-chunk streams.copy-chunk streams"), so the last component
    is dropped rather than parsed.
    """
    seen = set()
    for line in stream:
        line = line.strip()
        if not line.startswith("smb2."):
            continue
        name = line.rsplit(".", 1)[0]
        if name:
            seen.add(name)
    return sorted(seen)


def format_row(phase, rc, gate_hits, outcome, subtest):
    return f"{phase}|{rc}|{gate_hits}|{outcome}|{subtest}"


def read_results(path):
    """results.txt -> [(phase, rc, gate_hits, outcome, subtest)].

    rc is an int, or RC_NOLOG / RC_NOMARKER; it is diagnostic only.  The subtest
    is split off last so a name containing the delimiter cannot eat the columns
    before it.
    """
    rows = []
    with open(path, errors="replace") as fp:
        for lineno, line in enumerate(fp, 1):
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#"):
                continue
            parts = line.split("|", 4)
            if len(parts) != 5:
                raise ValueError(f"{path}:{lineno}: malformed row: {line!r}")
            phase, rc, gate_hits, outcome, subtest = parts
            if rc not in (RC_NOLOG, RC_NOMARKER):
                rc = int(rc)
            if outcome not in (OUT_PASS, OUT_FAIL, OUT_NOSTART, OUT_NOVERDICT):
                raise ValueError(f"{path}:{lineno}: unknown outcome {outcome!r}")
            rows.append((phase, rc, int(gate_hits), outcome, subtest))
    return rows


def collect(work, gate1, phase):
    """One phase's log tree -> results rows, one per subtest swept in it."""
    with open(os.path.join(work, f"subtests.{phase}.txt")) as fp:
        subtests = [l.strip() for l in fp if l.strip()]

    rows = []
    for subtest in subtests:
        path = os.path.join(work, "logs", phase, mangle(subtest) + ".log")
        if not os.path.exists(path):
            rows.append((phase, RC_NOLOG, 0, OUT_NOSTART, subtest))
            continue
        with open(path, errors="replace") as fp:
            body = fp.read()

        m = re.search(r"^runner-exit=(-?\d+)$", body, re.M)
        rc = int(m.group(1)) if m else RC_NOMARKER

        verdict = MARK_VERDICT.search(body)
        if verdict:
            outcome = OUT_PASS if verdict.group(1) == "PASSED" else OUT_FAIL
        elif MARK_STARTED in body:
            outcome = OUT_NOVERDICT
        else:
            outcome = OUT_NOSTART

        rows.append((phase, rc, body.count(gate1), outcome, subtest))
    return rows


def candidates(rows):
    """Which phase-off subtests are worth re-running with the feature on.

    A subtest that passed with it off and never reached the gate cannot be
    rescued by turning it on, so it is indifferent without a second run -- which
    is what keeps the second phase to a fraction of the catalog.

    Known gap: the gate in smb_proc_query_info.c logs nothing and returns no
    error, it substitutes a synthesized "::$DATA" answer.  A subtest that passes
    off the back of that and would fail with the feature on is invisible here.
    Widening phase 2 to the whole catalog would catch it, at roughly double the
    wall clock.
    """
    out = []
    for _phase, _rc, gate_hits, outcome, subtest in rows:
        if outcome not in CONCLUSIVE:
            continue
        if outcome == OUT_PASS and not gate_hits:
            continue
        out.append(subtest)
    return sorted(out)


def classify_pairs(rows):
    """results rows -> ({subtest: (class, out_off, out_on)}, inconclusive)."""
    off = [r for r in rows if r[0] == PHASE_OFF]
    on  = {r[4]: r[3] for r in rows if r[0] == PHASE_ON}

    classified, inconclusive = {}, []
    for _phase, _rc, gate_hits, out_off, subtest in off:
        if out_off not in CONCLUSIVE:
            inconclusive.append(subtest)
            continue

        if subtest not in on:
            # Not a candidate: it passed with the feature off and never reached
            # the gate, so there was nothing for a second run to change.
            classified[subtest] = (CLASS_INDIFFERENT, out_off, None)
            continue

        out_on = on[subtest]
        if out_on not in CONCLUSIVE:
            inconclusive.append(subtest)
            continue

        off_ok, on_ok = out_off == OUT_PASS, out_on == OUT_PASS
        if not off_ok and on_ok:
            cls = CLASS_NEEDS
        elif not off_ok:
            cls = CLASS_NEEDS_FAIL
        elif not on_ok:
            cls = CLASS_NEGATIVE
        else:
            cls = CLASS_INDIFFERENT
        classified[subtest] = (cls, out_off, out_on)

    return classified, sorted(inconclusive)


def stale_entries(patterns, classified):
    """List entries that cover nothing the sweep found a use for."""
    wanted = [s for s, (cls, _o, _n) in classified.items()
              if cls in LISTED_CLASSES]
    return [p for p in patterns if not any(covered(s, [p]) for s in wanted)]


def classify(rows, patterns, floor, scoped=False, out=sys.stdout):
    """Verdict over a two-phase sweep.  Returns the process exit status.

    0  the list agrees with what the sweep measured
    1  the list disagrees (drift; the sweep itself was sound)
    2  the sweep is untrustworthy, so no verdict was reached
    """
    classified, inconclusive = classify_pairs(rows)
    hit_gate = {s for p, _rc, h, _o, s in rows if p == PHASE_OFF and h}
    nolog = [s for p, rc, _h, _o, s in rows
             if rc == RC_NOLOG and p == PHASE_OFF]

    for cls in (CLASS_NEEDS, CLASS_NEEDS_FAIL, CLASS_NEGATIVE):
        members = sorted(s for s, (c, _o, _n) in classified.items() if c == cls)
        if not members:
            continue
        print(f"\n{CLASS_HEADING[cls]} ({len(members)}):", file=out)
        for s in members:
            is_covered = covered(s, patterns)
            # The marker flags the problem, and which direction is a problem
            # depends on the class: a listed negative test is as wrong as an
            # unlisted subtest that needs the feature.
            if cls == CLASS_NEGATIVE:
                mark = "!!!" if is_covered else "   "
            else:
                mark = "   " if is_covered else "+++"
            print(f"  {mark} {s}", file=out)

    # Everything below only means something if the sweep actually observed the
    # gate.  Without these checks an empty harvest -- wrong log level, driver
    # aborting at startup, netns wrapper broken, drifted gate string -- reports
    # agreement and exits 0.
    fatal = []
    if nolog:
        fatal.append(f"{len(nolog)} subtest(s) produced no log at all, e.g. "
                     + ", ".join(sorted(nolog)[:5]))
    if inconclusive:
        fatal.append(f"{len(inconclusive)} subtest(s) never reported a verdict -- "
                     "skipped, timed out, or died before saying anything -- so "
                     "their outcome is unknown, e.g. "
                     + ", ".join(inconclusive[:5]))
    if not hit_gate:
        fatal.append("the sweep observed the gate ZERO times; it tested nothing")
    elif not any(s.startswith(floor) for s in hit_gate):
        fatal.append(f"no '{floor}*' subtest hit the gate; those unambiguously "
                     "open a stream, so the sweep is not observing the gate "
                     "correctly")

    if fatal:
        print("\nSWEEP IS NOT TRUSTWORTHY -- not reporting drift:", file=out)
        for f in fatal:
            print(f"  ! {f}", file=out)
        return 2

    missing = sorted(s for s, (c, _o, _n) in classified.items()
                     if c in LISTED_CLASSES and not covered(s, patterns))
    forbidden = sorted(s for s, (c, _o, _n) in classified.items()
                       if c == CLASS_NEGATIVE and covered(s, patterns))
    # A scoped sweep measured a fraction of the catalog, so almost every entry
    # covers nothing it looked at.  Absence of evidence, not evidence of
    # staleness -- the other two directions stay sound because they rest on
    # subtests the sweep did measure.
    stale = [] if scoped else stale_entries(patterns, classified)

    if missing:
        print(f"\n{len(missing)} subtest(s) MISSING from the list:", file=out)
        for s in missing:
            print(f"    {s}", file=out)
        print("\nAdd them by hand (the file carries commentary worth keeping).",
              file=out)
    if forbidden:
        print(f"\n{len(forbidden)} subtest(s) are covered by the list but must "
              "NOT be:", file=out)
        for s in forbidden:
            print(f"    {s}", file=out)
        print("\nThese pass only while the feature is off.  Find the entry that\n"
              "covers each one and narrow it; do not widen the list further.",
              file=out)
    if stale:
        print(f"\n{len(stale)} list entr(y/ies) cover nothing that needs the "
              "feature:", file=out)
        for p in stale:
            print(f"    {p}", file=out)
        print("\nEither the catalog dropped them or they were never needed; "
              "remove them.", file=out)

    if missing or forbidden or stale:
        return 1

    print("\nThe list agrees with the sweep.", file=out)
    return 0


def read_expectations(path):
    """Known-answer rows for the smoke run: `<classification>|<subtest>`."""
    expected = {}
    with open(path) as fp:
        for lineno, line in enumerate(fp, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split("|", 1)
            if len(parts) != 2 or parts[0] not in CLASS_HEADING:
                raise ValueError(f"{path}:{lineno}: malformed expectation: {line!r}")
            expected[parts[1]] = parts[0]
    if not expected:
        raise ValueError(f"{path}: no expectations; a vacuous smoke run proves nothing")
    return expected


def check(rows, expected, out=sys.stdout):
    """Compare a real two-phase sweep against known classifications.

    The end-to-end counterpart to the fixture tests: it runs the real driver over
    subtests whose behaviour is already established, so machinery that has
    silently stopped observing anything cannot pass.
    """
    classified, inconclusive = classify_pairs(rows)
    bad = []

    for subtest, want in sorted(expected.items()):
        if subtest in inconclusive:
            bad.append(f"{subtest}: inconclusive (timed out, killed, or no log)")
            continue
        if subtest not in classified:
            bad.append(f"{subtest}: not in the sweep at all")
            continue
        got, out_off, out_on = classified[subtest]
        where = f"off={out_off} on={out_on or '-'}"
        if got != want:
            bad.append(f"{subtest}: expected {want}, got {got} ({where})")
        else:
            print(f"  ok  {subtest}: {got} ({where})", file=out)

    if bad:
        print(f"\n{len(bad)} known-answer subtest(s) did not behave as recorded:",
              file=out)
        for b in bad:
            print(f"  ! {b}", file=out)
        print("\nEither the sweep is broken, or the server's behaviour changed "
              "and the\nexpectations need re-recording.  Do not wire a sweep "
              "this stale to CI.", file=out)
        return 1

    print(f"\nAll {len(expected)} known-answer subtests behaved as recorded.",
          file=out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("catalog", help="smbtorture --list -> subtest names")

    p = sub.add_parser("collect", help="one phase's log tree -> results rows")
    p.add_argument("--work", required=True)
    p.add_argument("--gate", required=True)
    p.add_argument("--phase", required=True, choices=(PHASE_OFF, PHASE_ON))

    p = sub.add_parser("candidates", help="phase-off rows -> subtests to re-run")
    p.add_argument("--results", required=True)

    p = sub.add_parser("classify", help="results.txt -> drift verdict")
    p.add_argument("--results", required=True)
    p.add_argument("--list", required=True, dest="listfile")
    p.add_argument("--floor", required=True)
    p.add_argument("--scoped", action="store_true",
                   help="the sweep covered part of the catalog, so do not "
                        "report list entries it never measured as stale")

    p = sub.add_parser("names", help="expectations file -> subtest names")
    p.add_argument("--expect", required=True)

    p = sub.add_parser("check", help="results.txt -> known-answer verdict")
    p.add_argument("--results", required=True)
    p.add_argument("--expect", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "catalog":
        for name in read_catalog(sys.stdin):
            print(name)
        return 0

    if args.cmd == "collect":
        for row in collect(args.work, args.gate, args.phase):
            print(format_row(*row))
        return 0

    if args.cmd == "candidates":
        for name in candidates(read_results(args.results)):
            print(name)
        return 0

    if args.cmd == "names":
        for name in sorted(read_expectations(args.expect)):
            print(name)
        return 0

    if args.cmd == "classify":
        patterns = read_patterns(args.listfile)
        if not patterns:
            print(f"error: {args.listfile} yielded zero patterns; that would "
                  "turn ADS off\n       everywhere, which is never a plausible "
                  "answer", file=sys.stderr)
            return 2
        return classify(read_results(args.results), patterns, args.floor,
                        scoped=args.scoped)

    return check(read_results(args.results), read_expectations(args.expect))


if __name__ == "__main__":
    sys.exit(main())
