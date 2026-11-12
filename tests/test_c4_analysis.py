"""C4/W1 data-availability analysis: origins, REAL_TIME/PSEUDO classification, 1990-91 loss, variants.

The analysis is deterministic and works on two input layers:
  * committed metadata (protocol v4, protocols/evaluation_origins.json, protocols/nber_dates.json);
  * optional git-ignored local data (data/raw/<SERIES>/vintages.zip, data/raw/USREC/latest.csv).
The numbers that docs/76 quotes (W1: 121 REAL_TIME origins, 8 recession origins; W2: 18) must be
reproduced exactly, and the fallback path (no data/raw) must give the same counts.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import c4_analysis as c4  # noqa: E402

PROTOCOL_PATH = ROOT / "protocols" / "evaluation_protocol_v4.json"
DATA_PRESENT = (ROOT / "data" / "raw" / "USREC" / "latest.csv").exists()


class C4Analysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = c4.build_analysis()
        cls.protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))

    # --------------------------------------------------------------- origins and protocol inputs
    def test_origins_reconstruction_matches_the_recorded_grid(self):
        check = self.document["origins_cross_check"]
        self.assertTrue(check["ok"], check["mismatches"])
        self.assertEqual(check["pairs_checked"], 33)  # (4+4+3) horizons x 3 windows
        self.assertEqual(self.document["origins_source"]["sha256"],
                         c4.sha256_file(ROOT / "protocols" / "evaluation_origins.json"))

    def test_t1_origin_grid_matches_the_protocol_file(self):
        recorded = json.loads((ROOT / "protocols" / "evaluation_origins.json")
                              .read_text(encoding="utf-8"))["targets"]["T1_CPI_YOY"]
        self.assertEqual(recorded["W1"]["1"]["count"], 204)
        self.assertEqual(recorded["W1"]["1"]["first"]["origin"], "1990-01-31")
        self.assertEqual(recorded["W1"]["1"]["last"]["origin"], "2006-12-31")
        counts = self.document["targets"]["T1_CPI_YOY"]["W1"]["1"]["counts"]
        self.assertEqual(counts["origins"], 204)

    def test_rule_is_read_from_the_frozen_protocol(self):
        rule = self.document["rule"]
        self.assertEqual(rule["min_recession_months"],
                         self.protocol["admission"]["recession_conditioning"]
                         ["min_recession_months"])
        self.assertEqual(rule["min_recession_months"], 12)
        self.assertEqual(rule["applies_to"], ["T1_CPI_YOY", "T2_GDP_GROWTH"])
        self.assertIn("NBER recession month", rule["instrument"])
        self.assertEqual(self.document["protocol"]["sha256"],
                         c4.sha256_file(PROTOCOL_PATH))

    # ------------------------------------------------------------------- real-time classification
    def test_t1_w1_real_time_start_and_counts(self):
        counts = self.document["targets"]["T1_CPI_YOY"]["W1"]["1"]["counts"]
        self.assertEqual(counts["real_time_full"], 121)
        self.assertEqual(counts["pseudo_full"], 204 - 121)
        self.assertEqual(counts["first_real_time_origin"], "1996-12-31")
        # W2 is fully REAL_TIME
        w2 = self.document["targets"]["T1_CPI_YOY"]["W2"]["1"]["counts"]
        self.assertEqual(w2["real_time_full"], w2["origins"])
        self.assertEqual(w2["pseudo_full"], 0)

    def test_1990_91_origins_are_pseudo_and_blockers_are_exact(self):
        pair = self.document["targets"]["T1_CPI_YOY"]["W1"]["1"]
        self.assertEqual(pair["counts"]["first_real_time_origin"], "1996-12-31")
        blockers = self.document["lost_1990_91"]["by_target"]["T1_CPI_YOY"]["W1"]["1"]
        self.assertEqual(blockers["real_time_full"], 0)
        self.assertEqual(set(blockers["series_blockers"]),
                         {"CPILFESL", "GS10", "TB3MS"})
        self.assertEqual(blockers["series_blockers"]["CPILFESL"], "1996-12-12")
        self.assertEqual(blockers["series_blockers"]["GS10"], "1996-12-03")

    # ------------------------------------------------------------------------ C4 counts and masks
    def test_recession_counts_match_docs76(self):
        w1 = self.document["targets"]["T1_CPI_YOY"]["W1"]["1"]
        w2 = self.document["targets"]["T1_CPI_YOY"]["W2"]["1"]
        self.assertEqual(w1["c4_v4"]["recession_origins"], 8)      # docs/76: 8 < 12
        self.assertEqual(w2["c4_v4"]["recession_origins"], 18)     # docs/76: 18
        self.assertFalse(w1["c4_v4"]["met"])
        self.assertTrue(w2["c4_v4"]["met"])
        computed = self.document["cross_checks"]["computed"]
        self.assertEqual(computed["W1_T1_h1_origins"], 121)
        self.assertEqual(computed["W1_T1_h1_recession_origins"], 8)
        self.assertEqual(computed["W2_T1_h1_recession_origins"], 18)

    def test_mask_definitions_differ_and_h0_is_degenerate(self):
        w1 = self.document["targets"]["T1_CPI_YOY"]["W1"]
        self.assertEqual(w1["1"]["recession"]["origin_month"]["real_time_full"], 8)
        self.assertEqual(w1["1"]["recession"]["window_overlap"]["real_time_full"], 8)
        # for longer horizons the (t,t+h] overlap counts more origins than the origin-month reading
        self.assertEqual(w1["12"]["recession"]["origin_month"]["real_time_full"], 8)
        self.assertGreater(w1["12"]["recession"]["window_overlap"]["real_time_full"], 8)
        t2_w1 = self.document["targets"]["T2_GDP_GROWTH"]["W1"]["0"]
        self.assertEqual(t2_w1["recession"]["window_overlap"]["origins"], 0)
        self.assertGreater(t2_w1["recession"]["origin_month"]["origins"], 0)
        anomalies = {entry["pair"] for entry in self.document["mask_anomalies"]}
        self.assertIn("T2_GDP_GROWTH/W1/h0", anomalies)
        self.assertIn("T2_GDP_GROWTH/W2/h0", anomalies)

    def test_t2_is_structurally_unreachable_under_the_current_rule(self):
        for window in ("W1", "W2"):
            for horizon, pair in self.document["targets"]["T2_GDP_GROWTH"][window].items():
                self.assertFalse(pair["c4_v4"]["met"], (window, horizon))
                self.assertEqual(pair["c4_v4"]["status"], "INCONCLUSIVE")
        w2 = self.document["targets"]["T2_GDP_GROWTH"]["W2"]
        # the whole W2 recession is 18 months = 6 quarters -> fewer than 12 recession quarters ever
        for horizon in ("1", "2", "4"):
            self.assertLessEqual(w2[horizon]["recession"]["origin_month"]["real_time_full"], 7)
            self.assertLessEqual(w2[horizon]["recession"]["window_overlap"]["real_time_full"], 7)

    def test_c4_does_not_apply_to_t3(self):
        for window in ("W1", "W2"):
            for horizon, pair in self.document["targets"]["T3_RECESSION"][window].items():
                self.assertFalse(pair["c4_v4"]["applies"])
                self.assertEqual(pair["c4_v4"]["status"], "NOT_APPLICABLE_TO_TARGET")

    # ---------------------------------------------------------------------------------- 1990-91
    def test_1990_91_episode_is_exactly_lost(self):
        episode = self.document["lost_1990_91"]["episode"]
        self.assertEqual(episode["peak"], "1990-07")
        self.assertEqual(episode["trough"], "1991-03")
        self.assertEqual(episode["months"], 8)
        self.assertEqual(episode["month_labels"][0], "1990-08")
        self.assertEqual(episode["month_labels"][-1], "1991-03")
        affected = self.document["lost_1990_91"]["affected_series"]
        self.assertIn("CPILFESL", affected)
        self.assertIn("GDPC1", affected)
        self.assertIn("T1_CPI_YOY/W1/h1", affected["CPILFESL"]["pairs"])
        self.assertGreater(affected["CPILFESL"]["episode_origins_blocked"], 0)
        self.assertIn("1996-12", self.document["lost_1990_91"]["summary"])

    # ---------------------------------------------------------------------------------- variants
    def test_variants_are_consistent_with_the_pair_counts(self):
        variants = self.document["variants"]
        v0 = variants["V0_v4_current"]
        for target, windows in self.document["targets"].items():
            for window, horizons in windows.items():
                for horizon, pair in horizons.items():
                    key = f"{target}/{window}/{horizon}"
                    self.assertEqual(v0["window_overlap_mask"][key],
                                     pair["c4_v4"]["recession_origins"], key)
                    self.assertEqual(v0["origin_month_mask"][key],
                                     pair["recession"]["origin_month"]["real_time_full"], key)
        # PSEUDO allowance restores T1 W1 but not T2 W2
        v2 = variants["V2_allow_pseudo_for_c4"]
        self.assertTrue(v2["met_origin_month"]["T1_CPI_YOY/W1/1"])
        self.assertEqual(v2["counts_origin_month"]["T1_CPI_YOY"]["W1"]["1"], 16)
        self.assertFalse(v2["met_origin_month"]["T2_GDP_GROWTH/W2/1"])
        # archive extension fixes T1 W1 but not T2
        v4 = variants["V4_extend_vintage_archives"]
        self.assertTrue(v4["met_window_overlap"]["V4a_extend_rates_and_core_cpi"]["T1_CPI_YOY/W1/1"])
        self.assertFalse(v4["met_window_overlap"]["V4a_extend_rates_and_core_cpi"]
                         ["T2_GDP_GROWTH/W1/1"])
        # extending only the rates does not fix T1: CPILFESL is a co-blocker
        self.assertFalse(v4["met_window_overlap"]["V4c_extend_rates_only"]["T1_CPI_YOY/W1/1"])
        # lowering the global minimum to 8 fixes T1 W1 but not T2 W2
        self.assertTrue(variants["V3_relax_min_recession_origins"]["met_at_8"]["T1_CPI_YOY/W1/1"])
        self.assertFalse(variants["V3_relax_min_recession_origins"]["met_at_8"]
                         ["T2_GDP_GROWTH/W2/1"])
        # the recommendation is explicit and requires the owner
        self.assertTrue(self.document["recommendation"]["requires_owner_decision"])
        self.assertTrue(self.document["recommendation"]["requires_independent_review"])

    # --------------------------------------------------------------------- determinism, offline use
    def test_deterministic_and_reproducible_document(self):
        first = c4.build_analysis()
        second = c4.build_analysis()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertNotIn("generated_at", first, "the analysis must not depend on wall clock")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "c4_analysis.json"
            c4.write_analysis(first, out)
            ok, message, digest = c4.check_analysis(out)
            self.assertTrue(ok, message)
            self.assertEqual(digest, c4.sha256_file(out))
            # tampering is detected
            payload = json.loads(out.read_text(encoding="utf-8"))
            payload["summary"]["w1_t1_h1"]["recession_origins_real_time"] = 12
            out.write_text(json.dumps(payload), encoding="utf-8")
            ok, message, _ = c4.check_analysis(out)
            self.assertFalse(ok)
            self.assertIn("differs", message)

    def test_offline_fallback_gives_the_same_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "no-data"
            fallback = c4.build_analysis(empty)
        self.assertFalse(fallback["vintages"]["all_archives_available"])
        self.assertEqual(fallback["chronology"]["source"], "protocols/nber_dates.json")
        for target in ("T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION"):
            for window in ("W1", "W2"):
                document_horizons = self.document["targets"][target][window]
                for horizon in document_horizons:
                    self.assertEqual(fallback["targets"][target][window][horizon]["counts"],
                                     document_horizons[horizon]["counts"], (target, window, horizon))
                    self.assertEqual(
                        fallback["targets"][target][window][horizon]["c4_v4"]["recession_origins"],
                        document_horizons[horizon]["c4_v4"]["recession_origins"],
                        (target, window, horizon))
        self.assertEqual(fallback["summary"]["w1_t1_h1"],
                         self.document["summary"]["w1_t1_h1"])

    def test_nber_fallback_matches_usrec_when_data_present(self):
        if not DATA_PRESENT:
            self.skipTest("data/raw is git-ignored and not downloaded")
        usrec = c4.read_usrec(ROOT / "data" / "raw" / "USREC" / "latest.csv")
        fallback = c4.read_nber_reference_monthly()
        start = 12 * 1980
        end = 12 * 2021
        for index in range(start, end):
            self.assertEqual(usrec.get(index, 0) >= 0.5, fallback.get(index, 0) >= 0.5,
                             c4.label_of(index))

    def test_archive_metadata_matches_the_documented_first_vintages(self):
        archives = self.document["vintages"]["archives"]
        for series, documented in c4.DOCUMENTED_FIRST_VINTAGE.items():
            meta = archives[series]
            if not meta["available"]:
                continue
            self.assertEqual(meta["first_vintage"], documented, series)
            self.assertEqual(len(meta["sha256"]), 64, series)
            self.assertGreater(meta["vintages"], 100, series)
        # the classification always has a resolved first vintage, from the archive or the fallback
        for series in c4.ARCHIVE_SERIES:
            resolved = self.document["vintages"]["first_vintages"][series]
            self.assertEqual(resolved["first_vintage"], c4.DOCUMENTED_FIRST_VINTAGE[series])

    def test_frozen_files_are_only_read(self):
        # the analysis records the frozen hashes it read; the files themselves are unchanged inputs
        self.assertEqual(self.document["protocol"]["sha256"], c4.sha256_file(PROTOCOL_PATH))
        self.assertEqual(self.document["origins_source"]["sha256"],
                         c4.sha256_file(ROOT / "protocols" / "evaluation_origins.json"))
        composition = ROOT / "protocols" / "comparator_composition_2026-11-11.json"
        self.assertEqual(self.document["composition_source"]["sha256"],
                         c4.sha256_file(composition))
        source = (ROOT / "tools" / "c4_analysis.py").read_text(encoding="utf-8")
        for frozen in ("evaluation_protocol_v4.json", "r3_specification", "nf_sim_thresholds",
                       "comparator_composition"):
            self.assertNotIn(f"write_text({frozen}", source)
            self.assertNotIn(f"write_bytes({frozen}", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
