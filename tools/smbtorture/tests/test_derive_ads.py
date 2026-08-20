# SPDX-FileCopyrightText: 2024-2026 Chimera-NAS Project Contributors
#
# SPDX-License-Identifier: Unlicense
"""Unit tests for tools/smbtorture/derive_ads.py.

   These exist because the verdict logic used to live in a quoted heredoc inside
   derive_ads.sh, reachable only by running a 655-subtest sweep or by fabricating
   a log tree.  Fabrication was what happened, and a fabricated tree only ever
   contains what its author already believed a sweep produces -- so the belief
   that reaching the named-streams gate means needing the feature was never
   contradicted by a test.  It is contradicted here, and end to end by
   ads_smoke_expect.txt.
"""

import io, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import derive_ads

# HERE is tools/smbtorture/tests; the repo root is three levels up.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
REAL_LIST = os.path.join(REPO, "src", "server", "smb", "tests",
                         "smbtorture_ads_subtests.txt")
REAL_EXPECT = os.path.join(os.path.dirname(HERE), "ads_smoke_expect.txt")

FLOOR = "smb2.streams."
GATE1 = "named streams: disabled by config"

# The subtest this whole mechanism exists to protect: a negative test asserting
# that a server WITHOUT stream support rejects a stream path.  It reaches gate 1
# by design and passes because it does.
NEGATIVE = "smb2.create_no_streams.no_stream"

PASS, FAIL = derive_ads.OUT_PASS, derive_ads.OUT_FAIL


def off(name, outcome, gate=0, rc=0):
    return (derive_ads.PHASE_OFF, rc, gate, outcome, name)


def on(name, outcome, rc=0):
    return (derive_ads.PHASE_ON, rc, 0, outcome, name)


# A minimal sweep that satisfies the sanity floor, so a test can add the one row
# it cares about without every verdict coming back "untrustworthy".
def floor_rows():
    return [off("smb2.streams.io", FAIL, gate=1), on("smb2.streams.io", PASS)]


def verdict(rows, patterns, floor=FLOOR, scoped=False):
    out = io.StringIO()
    rc = derive_ads.classify(rows, patterns, floor, scoped=scoped, out=out)
    return rc, out.getvalue()


class TestCovered(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(derive_ads.covered("smb2.sdread", ["smb2.sdread"]))

    def test_parent_suite_prefix(self):
        self.assertTrue(derive_ads.covered("smb2.streams.io", ["smb2.streams"]))

    def test_the_dot_is_required(self):
        # The old strncmp(name, "smb2.streams.", 13) chain could never match
        # smb2.stream-inherit-perms -- no 's' before the hyphen -- and a prefix
        # rule without the '.' would over-match it instead.
        self.assertFalse(
            derive_ads.covered("smb2.stream-inherit-perms", ["smb2.streams"]))

    def test_match_is_one_directional(self):
        self.assertFalse(derive_ads.covered("smb2.fileid", ["smb2.fileid.fileid"]))

    def test_no_plausible_entry_covers_the_negative_test(self):
        self.assertFalse(
            derive_ads.covered(NEGATIVE, ["smb2.create", "smb2.streams"]))

    def test_shipped_list_does_not_cover_the_negative_test(self):
        patterns = derive_ads.read_patterns(REAL_LIST)
        self.assertTrue(patterns, "the shipped list parsed to zero patterns")
        self.assertFalse(derive_ads.covered(NEGATIVE, patterns))


class TestCatalog(unittest.TestCase):
    def test_drops_the_trailing_display_name(self):
        got = derive_ads.read_catalog(io.StringIO(
            "smb2.getinfo.complex.complex\nsmb2.scan.scan.scan\n"))
        self.assertEqual(got, ["smb2.getinfo.complex", "smb2.scan.scan"])

    def test_keeps_space_bearing_names_intact(self):
        got = derive_ads.read_catalog(io.StringIO(
            "smb2.ioctl.copy-chunk streams.copy-chunk streams\n"))
        self.assertEqual(got, ["smb2.ioctl.copy-chunk streams"])

    def test_ignores_non_smb2_and_dedupes(self):
        got = derive_ads.read_catalog(io.StringIO(
            "rpc.lsa.lsa\nsmb2.dir.find.find\nsmb2.dir.find.find\n"))
        self.assertEqual(got, ["smb2.dir.find"])


class TestResultsFile(unittest.TestCase):
    def _write(self, d, text):
        path = os.path.join(d, "results.txt")
        with open(path, "w") as fp:
            fp.write(text)
        return path

    def test_round_trip(self):
        row = off(NEGATIVE, PASS, gate=4)
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, derive_ads.format_row(*row) + "\n")
            self.assertEqual(derive_ads.read_results(path), [row])

    def test_subtest_keeps_a_delimiter(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, "off|1|2|fail|smb2.odd|name\n")
            self.assertEqual(derive_ads.read_results(path),
                             [("off", 1, 2, "fail", "smb2.odd|name")])

    def test_malformed_row_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, "off|0|nonsense\n")
            with self.assertRaises(ValueError):
                derive_ads.read_results(path)

    def test_unknown_outcome_is_loud(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, "off|0|0|maybe|smb2.dir.find\n")
            with self.assertRaises(ValueError):
                derive_ads.read_results(path)


class TestCollect(unittest.TestCase):
    STARTED = "Server started\n"

    def _work(self, d, phase, subtests, logs):
        os.makedirs(os.path.join(d, "logs", phase))
        with open(os.path.join(d, f"subtests.{phase}.txt"), "w") as fp:
            fp.write("".join(s + "\n" for s in subtests))
        for name, body in logs.items():
            with open(os.path.join(d, "logs", phase,
                                   derive_ads.mangle(name) + ".log"), "w") as fp:
                fp.write(body)
        return d

    def _one(self, body, name="smb2.streams.io", phase="off"):
        with tempfile.TemporaryDirectory() as d:
            self._work(d, phase, [name], {name: body})
            rows = derive_ads.collect(d, GATE1, phase)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_counts_gate_lines_and_reads_the_verdict(self):
        row = self._one(f"runner-exit=1\n{GATE1} 'a'\n{GATE1} 'b'\n"
                        + self.STARTED + "smbtorture: FAILED (exit code 1)\n")
        self.assertEqual(row, off("smb2.streams.io", FAIL, gate=2, rc=1))

    def test_a_verdict_survives_a_crash_after_it(self):
        # smb2.getinfo.complex does exactly this: it reaches its assertion,
        # reports FAILED, then aborts at teardown on a leaked evpl buffer, so
        # the process exits 134.  Scoring the status instead of the verdict
        # would have called a reached conclusion a crash.
        row = self._one(f"runner-exit=134\n{GATE1}\n" + self.STARTED
                        + "smbtorture: FAILED (exit code 1)\n")
        self.assertEqual(row[1], 134)
        self.assertEqual(row[3], FAIL)

    def test_started_but_no_verdict_is_inconclusive(self):
        row = self._one("runner-exit=137\n" + self.STARTED)
        self.assertEqual(row[3], derive_ads.OUT_NOVERDICT)

    def test_never_started_is_inconclusive(self):
        row = self._one("runner-exit=77\n")
        self.assertEqual(row[3], derive_ads.OUT_NOSTART)

    def test_a_missing_log_is_not_a_quiet_pass(self):
        with tempfile.TemporaryDirectory() as d:
            self._work(d, "off", ["smb2.dir.find"], {})
            self.assertEqual(derive_ads.collect(d, GATE1, "off"),
                             [("off", derive_ads.RC_NOLOG, 0,
                               derive_ads.OUT_NOSTART, "smb2.dir.find")])

    def test_space_bearing_name_round_trips_through_the_mangling(self):
        name = "smb2.ioctl.copy-chunk streams"
        row = self._one(f"runner-exit=1\n{GATE1}\n" + self.STARTED
                        + "smbtorture: FAILED (exit code 1)\n", name=name)
        self.assertEqual(row[4], name)


class TestCandidates(unittest.TestCase):
    def test_a_clean_pass_that_never_reached_the_gate_is_skipped(self):
        self.assertEqual(derive_ads.candidates([off("smb2.dir.find", PASS)]), [])

    def test_a_pass_that_reached_the_gate_is_a_candidate(self):
        # The negative test: passing is exactly what makes it interesting.
        self.assertEqual(derive_ads.candidates([off(NEGATIVE, PASS, gate=4)]),
                         [NEGATIVE])

    def test_a_failure_is_a_candidate(self):
        self.assertEqual(derive_ads.candidates([off("smb2.dir.find", FAIL)]),
                         ["smb2.dir.find"])

    def test_an_inconclusive_run_is_not_re_run(self):
        rows = [off("smb2.dir.find", derive_ads.OUT_NOSTART)]
        self.assertEqual(derive_ads.candidates(rows), [])


class TestClassifyPairs(unittest.TestCase):
    def _cls(self, out_off, out_on, gate=1):
        rows = [off("smb2.x.y", out_off, gate=gate)]
        if out_on is not None:
            rows.append(on("smb2.x.y", out_on))
        classified, _ = derive_ads.classify_pairs(rows)
        return classified["smb2.x.y"][0]

    def test_fail_then_pass_needs_ads(self):
        self.assertEqual(self._cls(FAIL, PASS), derive_ads.CLASS_NEEDS)

    def test_fail_then_fail_needs_ads_and_still_fails(self):
        self.assertEqual(self._cls(FAIL, FAIL), derive_ads.CLASS_NEEDS_FAIL)

    def test_pass_then_fail_is_a_negative_test(self):
        self.assertEqual(self._cls(PASS, FAIL), derive_ads.CLASS_NEGATIVE)

    def test_pass_then_pass_is_indifferent(self):
        self.assertEqual(self._cls(PASS, PASS), derive_ads.CLASS_INDIFFERENT)

    def test_never_re_run_is_indifferent(self):
        self.assertEqual(self._cls(PASS, None, gate=0),
                         derive_ads.CLASS_INDIFFERENT)


class TestClassify(unittest.TestCase):
    def test_agreement_is_clean(self):
        rc, _ = verdict(floor_rows(), ["smb2.streams"])
        self.assertEqual(rc, 0)

    def test_negative_test_is_not_reported_as_drift(self):
        """The defect this replaced the gate-hit criterion to fix.

        smb2.create_no_streams.no_stream reaches gate 1 four times and passes.
        Keyed on the gate it was drift, and the printed remedy turned its PASS
        into a FAIL with OBJECT_NAME_NOT_FOUND.  Keyed on the outcome it is a
        negative test, and the shipped list correctly does not carry it.
        """
        rows = floor_rows() + [off(NEGATIVE, PASS, gate=4), on(NEGATIVE, FAIL)]
        # Scoped: these two rows are not a full sweep, so the entries they did
        # not measure are not evidence of staleness.
        rc, text = verdict(rows, derive_ads.read_patterns(REAL_LIST), scoped=True)
        self.assertEqual(rc, 0, text)

    def test_uncovered_subtest_that_needs_ads_is_drift(self):
        rows = floor_rows() + [off("smb2.fileid.fileid", FAIL, gate=1),
                               on("smb2.fileid.fileid", PASS)]
        rc, text = verdict(rows, ["smb2.streams"])
        self.assertEqual(rc, 1)
        self.assertIn("MISSING", text)
        self.assertIn("smb2.fileid.fileid", text)

    def test_still_failing_subtest_still_belongs_in_the_list(self):
        rows = floor_rows() + [off("smb2.getinfo.complex", FAIL, gate=1),
                               on("smb2.getinfo.complex", FAIL)]
        rc, text = verdict(rows, ["smb2.streams"])
        self.assertEqual(rc, 1)
        self.assertIn("smb2.getinfo.complex", text)

    def test_covering_a_negative_test_is_drift_in_the_other_direction(self):
        # The direction the gate-hit criterion could never see, and the one that
        # actually breaks a passing test on all six backends.
        rows = floor_rows() + [off(NEGATIVE, PASS, gate=4), on(NEGATIVE, FAIL)]
        rc, text = verdict(rows, ["smb2.streams", "smb2.create_no_streams"])
        self.assertEqual(rc, 1)
        self.assertIn("must\nNOT be", text.replace(" NOT", "\nNOT"))
        self.assertIn(NEGATIVE, text)

    def test_an_entry_covering_nothing_is_stale(self):
        rc, text = verdict(floor_rows(), ["smb2.streams", "smb2.long-gone"])
        self.assertEqual(rc, 1)
        self.assertIn("smb2.long-gone", text)

    def test_a_scoped_sweep_does_not_cry_stale(self):
        # A --filter run measured a fraction of the catalog, so almost every
        # entry covers nothing it looked at.  Absence of evidence.
        rc, _ = verdict(floor_rows(), ["smb2.streams", "smb2.sdread"],
                        scoped=True)
        self.assertEqual(rc, 0)

    def test_zero_gate_hits_is_not_a_pass(self):
        # An empty harvest -- wrong log level, driver aborting at startup, netns
        # wrapper broken, drifted gate string -- must never read as agreement.
        rc, text = verdict([off("smb2.dir.find", PASS)], ["smb2.streams"])
        self.assertEqual(rc, 2)
        self.assertIn("ZERO times", text)

    def test_missing_floor_is_not_a_pass(self):
        rows = [off("smb2.sdread", FAIL, gate=1), on("smb2.sdread", PASS)]
        rc, text = verdict(rows, ["smb2.sdread"])
        self.assertEqual(rc, 2)
        self.assertIn(FLOOR, text)

    def test_an_unknown_outcome_beats_a_clean_verdict(self):
        for bad in (derive_ads.OUT_NOSTART, derive_ads.OUT_NOVERDICT):
            with self.subTest(outcome=bad):
                rows = floor_rows() + [off("smb2.dir.find", bad)]
                rc, text = verdict(rows, ["smb2.streams"])
                self.assertEqual(rc, 2)
                self.assertIn("never reported a verdict", text)

    def test_a_missing_log_beats_a_clean_verdict(self):
        rows = floor_rows() + [
            ("off", derive_ads.RC_NOLOG, 0, derive_ads.OUT_NOSTART, "smb2.dir.find")]
        rc, text = verdict(rows, ["smb2.streams"])
        self.assertEqual(rc, 2)
        self.assertIn("no log", text)


class TestSmokeExpectations(unittest.TestCase):
    def test_shipped_expectations_pin_the_negative_test(self):
        expected = derive_ads.read_expectations(REAL_EXPECT)
        self.assertEqual(expected.get(NEGATIVE), derive_ads.CLASS_NEGATIVE)

    def test_shipped_expectations_cover_all_four_classifications(self):
        # A smoke set that never exercises a row cannot notice that row breaking.
        self.assertEqual(set(derive_ads.read_expectations(REAL_EXPECT).values()),
                         set(derive_ads.CLASS_HEADING))

    def test_a_vacuous_expectations_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "expect.txt")
            with open(path, "w") as fp:
                fp.write("# nothing but commentary\n")
            with self.assertRaises(ValueError):
                derive_ads.read_expectations(path)

    def test_an_unknown_classification_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "expect.txt")
            with open(path, "w") as fp:
                fp.write("probably-fine|smb2.dir.find\n")
            with self.assertRaises(ValueError):
                derive_ads.read_expectations(path)

    def test_check_passes_when_the_sweep_matches(self):
        rows = [off(NEGATIVE, PASS, gate=4), on(NEGATIVE, FAIL)]
        out = io.StringIO()
        rc = derive_ads.check(rows, {NEGATIVE: derive_ads.CLASS_NEGATIVE}, out=out)
        self.assertEqual(rc, 0)

    def test_check_fails_when_the_classification_changes(self):
        rows = [off(NEGATIVE, PASS, gate=4), on(NEGATIVE, PASS)]
        out = io.StringIO()
        rc = derive_ads.check(rows, {NEGATIVE: derive_ads.CLASS_NEGATIVE}, out=out)
        self.assertEqual(rc, 1)
        self.assertIn("expected negative, got indifferent", out.getvalue())

    def test_check_fails_when_a_pinned_subtest_vanishes(self):
        out = io.StringIO()
        rc = derive_ads.check([], {NEGATIVE: derive_ads.CLASS_NEGATIVE}, out=out)
        self.assertEqual(rc, 1)
        self.assertIn("not in the sweep", out.getvalue())


if __name__ == "__main__":
    unittest.main()
