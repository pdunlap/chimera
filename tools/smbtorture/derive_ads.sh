#!/bin/bash
# SPDX-FileCopyrightText: 2024-2026 Chimera-NAS Project Contributors
#
# SPDX-License-Identifier: Unlicense
#
# Rediscover which smbtorture subtests need named-stream (ADS) support, and
# report any that are missing from src/server/smb/tests/smbtorture_ads_subtests.txt.
#
# Why this exists: that list used to be a hand-maintained strcmp chain in
# smbtorture_test.c, grown one entry at a time whenever somebody noticed a
# failure.  Eleven subtests were never noticed -- they open a stream as
# incidental setup (a second handle, an elaborate file or directory to query)
# and died in that setup with NT_STATUS_OBJECT_NAME_INVALID, looking exactly
# like a server bug.
#
# The set cannot be derived statically: there is no Samba source in this tree,
# and smbtorture builds its paths at run time from format strings, so scanning
# the binary yields noise.  So derive it empirically -- run each subtest with
# named streams forced OFF, run the interesting ones again with it forced ON,
# and classify each on whether the feature changed its outcome:
#
#   off    on     meaning                              belongs in the list?
#   ----   ----   ----------------------------------   --------------------
#   fail   pass   needs the feature                    yes
#   fail   fail   needs it to reach its real failure   yes, still failing
#   pass   fail   negative test; requires it OFF       NEVER
#   pass   pass   indifferent                          no; stale if listed
#
# Only the first phase sweeps the whole catalog.  The second re-runs just the
# subtests that failed or reached the gate, which is a fraction of it.
#
# This used to key on the gate-1 log line alone, which cannot tell row 1 from
# row 3: smb2.create_no_streams.no_stream trips that gate four times and passes
# BECAUSE it does, so it was reported as drift forever, and the remedy printed
# alongside would have broken it on all six backends.  Rows 3 and 4 were
# invisible entirely, so an over-broad or stale entry was never caught.  The
# gate line is still collected -- it is what proves the sweep observed anything,
# and it picks the second phase's candidates -- but it decides nothing.  It logs
# at debug, so the sweep raises the driver's level with SMBTORTURE_LOG_LEVEL.
#
# Runs against memfs only: it is the sole backend advertising
# CHIMERA_VFS_CAP_NAMED_STREAMS, so it is the only one where these subtests
# could pass with the feature on.
#
# Pre-requisites: smbtorture in PATH, chimera built with smbtorture_test (see
# --build-dir), run as root (the netns wrapper needs CAP_NET_ADMIN).
#
# This script is only the sweep.  The verdict lives in derive_ads.py beside it,
# and the two meet at one file, $WORK/results.txt -- so the verdict can be
# exercised from fixtures in milliseconds instead of from a 655-subtest sweep.
# Run --smoke first: it sweeps a handful of subtests whose behaviour is already
# recorded, end to end, in a couple of seconds.  A full sweep takes 60-90 minutes
# at the default parallelism and should not be the first thing you run.
#
# Output: prints the derived list, and diffs it against the checked-in file.
# Does not rewrite the file: the list carries hand-written commentary (which
# subtests still fail for real reasons, and why), and clobbering that is worse
# than the drift it would fix.  Add new entries by hand.
#
# Exit status, so CI can gate on it:
#   0  the checked-in list agrees with what the sweep measured
#   1  the list disagrees -- an entry is missing, or covers a subtest that must
#      stay off, or covers nothing at all (drift; the sweep itself was sound)
#   2  no drift verdict was reached, so nothing has been proved about the list.
#      Either the sweep is untrustworthy -- it observed the gate zero times, or
#      missed the subtests that unambiguously open a stream, or some subtest
#      timed out / was killed / produced no log -- or the run never started at
#      all (bad options, nothing built, no smbtorture).  A setup failure is not
#      drift, and must never be reported as one.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
LIST=$REPO/src/server/smb/tests/smbtorture_ads_subtests.txt
EXPECT=$HERE/ads_smoke_expect.txt
WORK=${SMBTORTURE_ADS_WORK:-/tmp/smbtorture-ads}
PARALLEL=${SMBTORTURE_PARALLEL:-12}
BUILD_DIR=${SMBTORTURE_BUILD_DIR:-}
FILTER=${SMBTORTURE_FILTER:-}
SMOKE=0

# The log line emitted by gate 1 in chimera_smb_create().  Threaded into both the
# sweep and the harvest below rather than duplicated -- a stale copy would make
# the sweep find nothing, which used to look exactly like success.
GATE1='named streams: disabled by config'

# Subtests that unambiguously open a named stream, used as a sanity floor: if the
# sweep does not see the gate fire for these, it is broken and its "nothing is
# missing" verdict is worthless.  Any of them may vanish from the catalog on a
# Samba upgrade, so the floor requires at least one, not all.
FLOOR='smb2.streams.'

usage()
{
    cat <<'USAGE'
usage: derive_ads.sh [options]

  --smoke              sweep only the subtests in ads_smoke_expect.txt and check
                       them against their recorded behaviour, instead of
                       reporting drift.  Seconds; run this first.
  --filter <regex>     only sweep catalog subtests matching this extended regex
                       ($SMBTORTURE_FILTER)
  --build-dir <dir>    build tree holding src/server/smb/tests/smbtorture_test
                       (default: first of $SMBTORTURE_BUILD_DIR, ./build/Release,
                       /build/Release that exists)
  --work-dir <dir>     scratch tree, recursively deleted at startup; must be
                       under /tmp (default /tmp/smbtorture-ads,
                       $SMBTORTURE_ADS_WORK)
  --parallel <n>       concurrent subtests (default 12, $SMBTORTURE_PARALLEL)
  -h, --help           this message
USAGE
}

# Every failure below is exit 2, never 1: exit 1 means a sweep ran and found the
# list wanting.  A run that never started has proved nothing about the list, and
# a CI job written to that contract would tell a developer who forgot to build
# that the ADS list had drifted.
die()
{
    echo "error: $1" >&2
    shift
    for line in "$@"; do
        echo "       $line" >&2
    done
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --smoke)       SMOKE=1; shift;;
        --filter)      FILTER=$2; shift 2;;
        --build-dir)   BUILD_DIR=$2; shift 2;;
        --work-dir)    WORK=$2; shift 2;;
        --parallel)    PARALLEL=$2; shift 2;;
        -h|--help)     usage; exit 0;;
        *)             echo "error: unknown option '$1'" >&2; usage >&2; exit 2;;
    esac
done

if [ "$SMOKE" -eq 1 ] && [ -n "$FILTER" ]; then
    die "--smoke and --filter both scope the sweep; pick one"
fi

case $PARALLEL in
    ''|*[!0-9]*) die "--parallel wants a positive integer (got '$PARALLEL')";;
esac
if [ "$PARALLEL" -lt 1 ]; then
    die "--parallel wants a positive integer (got '$PARALLEL')" \
        "xargs reads 0 as 'run every subtest at once'"
fi

# WORK is recursively deleted below, so refuse an override that does not name a
# fresh directory under /tmp.  '..' is rejected outright rather than resolved:
# /tmp/../root is under /tmp by prefix and nowhere near it in fact, and the
# pre-requisites above say to run this as root.
case $WORK in
    *..*)    die "SMBTORTURE_ADS_WORK must not contain '..' (got '$WORK')";;
    /tmp/?*) ;;
    *)       die "SMBTORTURE_ADS_WORK must name a directory under /tmp (got '$WORK')";;
esac

cd "$REPO"

# Build trees live in ./build/Release for worktrees and /build/Release for the
# main tree, so there is no single relative path that works in both.  Same probe
# as regen.sh, which is where the claim that the two share pre-requisites came
# from -- this script used to hardcode ./build/Release and abort with "not
# built" on a tree that was built.
if [ -z "$BUILD_DIR" ]; then
    for d in build/Release /build/Release; do
        if [ -x "$d/src/server/smb/tests/smbtorture_test" ]; then
            BUILD_DIR=$d
            break
        fi
    done
fi
DRIVER=$BUILD_DIR/src/server/smb/tests/smbtorture_test
if [ -z "$BUILD_DIR" ] || [ ! -x "$DRIVER" ]; then
    die "smbtorture_test not found (looked for $DRIVER)" \
        "run 'make release' first, or pass --build-dir"
fi
if ! command -v smbtorture >/dev/null; then
    die "smbtorture client not in PATH (install samba-testsuite)"
fi

# The probe yields a path relative to the repo for a worktree and an absolute
# one for the main tree; the runner cds elsewhere, so pin it down here.
ADS_DRIVER=$(cd "$(dirname "$DRIVER")" && pwd)/$(basename "$DRIVER")

# Any unplanned abort is "no verdict", never "drift".  set -e can propagate a 1
# from anywhere, and 1 is the one status this script must not invent.
trap 'rc=$?; if [ "$rc" -eq 1 ]; then rc=2; fi; exit $rc' ERR

rm -rf "$WORK"
mkdir -p "$WORK/logs" "$WORK/full"

# 1) Catalog.  Written to a file before being filtered, rather than piped: under
#    pipefail a `smbtorture --list | grep '^smb2\.'` that matches nothing makes
#    grep exit 1 and takes the whole script with it, so the diagnostic for an
#    empty catalog could never run for the case that produces one.
if ! smbtorture --list > "$WORK/list.raw" 2>&1; then
    sed -n '1,5p' "$WORK/list.raw" >&2
    die "'smbtorture --list' failed (first lines above)"
fi
python3 "$HERE/derive_ads.py" catalog < "$WORK/list.raw" > "$WORK/catalog.txt"
if [ ! -s "$WORK/catalog.txt" ]; then
    die "'smbtorture --list' yielded no smb2.* subtests; nothing to sweep"
fi
echo "catalog: $(wc -l < "$WORK/catalog.txt") subtests"

if [ "$SMOKE" -eq 1 ]; then
    python3 "$HERE/derive_ads.py" names --expect "$EXPECT" > "$WORK/wanted.txt"
    grep -Fx -f "$WORK/wanted.txt" "$WORK/catalog.txt" > "$WORK/subtests.txt" || true
    if [ "$(wc -l < "$WORK/subtests.txt")" -ne "$(wc -l < "$WORK/wanted.txt")" ]; then
        comm -23 <(sort "$WORK/wanted.txt") <(sort "$WORK/subtests.txt") >&2
        die "the subtests above are pinned in $EXPECT but absent from the catalog" \
            "a Samba upgrade changed the catalog; re-record the expectations"
    fi
elif [ -n "$FILTER" ]; then
    # Tell a bad regex apart from one that simply matched nothing: grep says 1
    # for no match and >1 for a malformed pattern, and conflating them sends you
    # looking for a catalog that does not contain what you asked for instead of
    # at your regex.  Same split as regen.sh:200.
    grep -E "$FILTER" "$WORK/catalog.txt" > "$WORK/subtests.txt" && grc=0 || grc=$?
    if [ "$grc" -gt 1 ]; then
        die "--filter '$FILTER' is not a valid extended regex"
    fi
    if [ ! -s "$WORK/subtests.txt" ]; then
        die "--filter '$FILTER' matched no catalog subtest"
    fi
else
    cp "$WORK/catalog.txt" "$WORK/subtests.txt"
fi
echo "sweeping: $(wc -l < "$WORK/subtests.txt") subtests"

# 2) Sweep.  One subtest per server process so an outcome is attributable to
#    exactly one subtest, and every subtest is swept pass or fail: a subtest can
#    reach the gate on a path it does not assert on and still pass, which is
#    exactly the case the outcome comparison exists to tell apart.
#
#    The runner reads its parameters from the environment rather than having
#    them spliced in with sed: a repo or work path containing '&' or the sed
#    delimiter would otherwise corrupt every runner, and the failure would look
#    like a broken sweep rather than a quoting bug.  regen.sh documents the same
#    choice.
RUNNER=$WORK/runner.sh
cat > "$RUNNER" <<'RUNNER_EOF'
#!/bin/bash
set -u
SUITE="$1"
SAFE="${SUITE//[^A-Za-z0-9]/_}"
FULL="$ADS_WORK/full/$SAFE.$ADS_PHASE.out"
cd "$ADS_REPO"
SMBTORTURE_LOG_LEVEL=debug SMBTORTURE_ADS="$ADS_PHASE" timeout --signal=KILL 300 \
    "$ADS_REPO/scripts/netns_test_wrapper.sh" "$ADS_DRIVER" -b memfs "$SUITE" \
    > "$FULL" 2>&1
rc=$?
# Distil rather than keep: the driver's own liveness and verdict markers, which
# the classification is computed from, plus the exit status for diagnosis and
# the gate lines as evidence the sweep observed anything.  Debug-level logs for
# the whole catalog would otherwise run to many GB.
{
    echo "runner-exit=$rc"
    grep -F "$ADS_GATE1" "$FULL" || true
    grep -E '^(Server started|smbtorture: (PASSED|FAILED))' "$FULL" || true
} > "$ADS_WORK/logs/$ADS_PHASE/$SAFE.log"
rm -f "$FULL"
exit 0
RUNNER_EOF
chmod +x "$RUNNER"
export ADS_WORK="$WORK" ADS_REPO="$REPO" ADS_GATE1="$GATE1" ADS_DRIVER

# One subtest per line, and -d '\n' so xargs honours that.  Eight catalog names
# contain spaces -- "smb2.ioctl.copy-chunk streams" and the smb2.charset /
# delete-on-close-perms display names -- and xargs' default blank-and-newline
# splitting would dispatch each of them as two nonexistent subtests, so the real
# name would never be swept and could never show up as missing -- leaving the
# sweep blind to exactly the kind of name it exists to find.  -d also turns off
# xargs' quote and backslash processing, which those same names would otherwise
# be subject to.
sweep()
{
    local phase=$1 subs=$2 n
    n=$(wc -l < "$subs")
    mkdir -p "$WORK/logs/$phase"
    echo "phase $phase: sweeping $n subtest(s) with SMBTORTURE_ADS=$phase at parallel=$PARALLEL"
    if [ "$n" -gt 0 ]; then
        ADS_PHASE=$phase xargs -d '\n' -P "$PARALLEL" -n 1 -a "$subs" "$RUNNER"
    fi
    python3 "$HERE/derive_ads.py" collect --work "$WORK" --gate "$GATE1" \
        --phase "$phase" >> "$WORK/results.txt"
}

: > "$WORK/results.txt"
cp "$WORK/subtests.txt" "$WORK/subtests.off.txt"
sweep off "$WORK/subtests.off.txt"

# Only the subtests that failed or reached the gate can have their outcome
# changed by turning the feature on; the rest are indifferent without a second
# run.  This is what keeps phase 2 to a fraction of the catalog.
python3 "$HERE/derive_ads.py" candidates --results "$WORK/results.txt" \
    > "$WORK/subtests.on.txt"
sweep on "$WORK/subtests.on.txt"

rmdir "$WORK/full" 2>/dev/null || true
echo "results: $WORK/results.txt"

# 3) Rule on the pair of outcomes per subtest.
rc=0
if [ "$SMOKE" -eq 1 ]; then
    python3 "$HERE/derive_ads.py" check \
        --results "$WORK/results.txt" --expect "$EXPECT" || rc=$?
else
    SCOPED=
    if [ -n "$FILTER" ]; then
        SCOPED=--scoped
    fi
    python3 "$HERE/derive_ads.py" classify --results "$WORK/results.txt" \
        --list "$LIST" --floor "$FLOOR" $SCOPED || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo
        echo "A single subtest can be checked by hand without a full sweep;"
        echo "run it both ways and compare:"
        echo "  for phase in off on; do SMBTORTURE_LOG_LEVEL=debug \\"
        echo "    SMBTORTURE_ADS=\$phase scripts/netns_test_wrapper.sh \\"
        echo "    $DRIVER -b memfs SUBTEST >/dev/null 2>&1;"
        echo "    echo \"\$phase: \$?\"; done"
    fi
fi
exit $rc
