"""Tests for the module registry (G2, docs/157): passports, statuses, append-only, export, tool."""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from amfe.learning import module_registry as mr  # noqa: E402
import register_modules  # noqa: E402

#: every passport the tool registers (G2 core + G5 gas/storage + world blocks 15/17),
#: module_id -> kind
EXPECTED = {
    "oil.brent": "market", "gas.lng_eu": "market", "oil.storage_curve": "market",
    "AGRI_SOY": "agri", "markets.finance": "finance", "agentic.sim": "agentic",
    "world.p2": "world", "world.institutions": "world", "world.technology": "world",
}


def passport(**overrides) -> dict:
    base = dict(
        module_id="demo.module", kind="market", version="0.1.0",
        display_name="Demo module", summary="A demo module for tests.",
        owner="track-x", code_sha256=mr.sha256_text("demo-code"), data_sha256=None,
        data_status="SYNTHETIC", created_at="2026-11-16",
        docs=["docs/157_Реестр_модулей.md"], tests=["tests/test_module_registry.py"],
        entrypoints=["demo.module"], limitations=["research prototype only"],
        components=["part_a"], code_files={"src/demo.py": mr.sha256_text("file")},
        status="RESEARCH_ONLY", note="test",
    )
    base.update(overrides)
    return mr.build_passport(**base)


REVIEW = {"reviewer": "independent-reviewer", "independent": True, "pass": True,
          "reviewed_at": "2026-11-16", "report_sha256": mr.sha256_text("review-report")}
CRITERIA = {"registered_at": "2026-11-01",
            "criteria": {"min_walkforward_folds": 12, "min_q": 0.6}}


class Passports(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "modules.jsonl"

    def test_register_and_append_only(self):
        first = passport()
        mr.register(self.path, first)
        prefix = self.path.read_bytes()
        mr.register(self.path, passport(module_id="demo.other", code_sha256=mr.sha256_text("x")))
        content = self.path.read_bytes()
        self.assertTrue(content.startswith(prefix))
        lines = [line for line in content.decode("utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        for line in lines:
            entry = json.loads(line)
            self.assertEqual(entry["schema"], mr.SCHEMA)
            self.assertEqual(line, json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":")))

    def test_duplicate_registration_fails(self):
        mr.register(self.path, passport())
        with self.assertRaises(mr.ModuleRegistryError):
            mr.register(self.path, passport())
        self.assertEqual(len(mr.load_entries(self.path)), 1)

    def test_status_on_registration_is_bounded(self):
        with self.assertRaises(mr.ModuleRegistryError):
            passport(status="LIMITED")
        with self.assertRaises(mr.ModuleRegistryError):
            passport(status="ACTIVE")
        with self.assertRaises(mr.ModuleRegistryError):
            passport(criteria=CRITERIA)
        self.assertEqual(passport(status="CANDIDATE")["status"], "CANDIDATE")

    def test_invalid_passports_fail_closed(self):
        for overrides in (
            {"kind": "unknown"}, {"version": "1"}, {"version": "0.1.0.1"},
            {"code_sha256": "short"}, {"docs": []}, {"tests": []}, {"limitations": []},
            {"owner": "  "}, {"created_at": "16.11.2026"}, {"status": "BOGUS"},
            {"data_status": "NONE", "data_sha256": mr.sha256_text("data")},
            {"data_sha256": "not-a-hash"},
        ):
            with self.assertRaises(mr.ModuleRegistryError, msg=str(overrides)):
                passport(**overrides)


class Statuses(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "modules.jsonl"
        mr.register(self.path, passport(status="CANDIDATE"))

    def test_above_candidate_requires_criteria_and_review(self):
        with self.assertRaises(mr.ModuleRegistryError) as context:
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16")
        self.assertIn("criteria", str(context.exception))
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=CRITERIA)
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", review=REVIEW)
        self.assertEqual(mr.state(self.path)["demo.module"]["status"], "CANDIDATE")

    def test_promotion_with_criteria_and_review(self):
        mr.set_status(self.path, module_id="demo.module", version="0.1.0", to_status="LIMITED",
                      at="2026-11-16", criteria=CRITERIA, review=REVIEW)
        module = mr.state(self.path)["demo.module"]
        self.assertEqual(module["status"], "LIMITED")
        record = module["versions"]["0.1.0"]
        self.assertEqual(record["criteria"]["sha256"],
                         mr.sha256_text(mr._canonical(dict(CRITERIA["criteria"]))))
        self.assertEqual(record["independent_review"]["reviewer"], "independent-reviewer")
        self.assertTrue(mr.validate_registry(self.path)["valid"])

    def test_promotion_gate_details(self):
        late = {"registered_at": "2026-12-01", "criteria": {"min_q": 0.6}}
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=late, review=REVIEW)
        self_review = dict(REVIEW, reviewer="track-x")
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=CRITERIA,
                          review=self_review)
        non_independent = dict(REVIEW, independent=False)
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=CRITERIA,
                          review=non_independent)
        failed = dict(REVIEW, **{"pass": False})
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="LIMITED", at="2026-11-16", criteria=CRITERIA,
                          review=failed)

    def test_demotion_and_same_status(self):
        mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                      to_status="RESEARCH_ONLY", at="2026-11-16")
        self.assertEqual(mr.state(self.path)["demo.module"]["status"], "RESEARCH_ONLY")
        with self.assertRaises(mr.ModuleRegistryError):
            mr.set_status(self.path, module_id="demo.module", version="0.1.0",
                          to_status="RESEARCH_ONLY", at="2026-11-16")


class ReadsAndExport(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "modules.jsonl"
        mr.register(self.path, passport(module_id="b.module", kind="agri",
                                        code_sha256=mr.sha256_text("b")))
        mr.register(self.path, passport(module_id="a.module", kind="market", version="0.2.0",
                                        code_sha256=mr.sha256_text("a2")))
        mr.register(self.path, passport(module_id="a.module", kind="market", version="0.1.0",
                                        code_sha256=mr.sha256_text("a1")))

    def test_latest_version_and_find(self):
        modules = mr.state(self.path)
        self.assertEqual(modules["a.module"]["latest_version"], "0.2.0")
        self.assertEqual(modules["a.module"]["versions"]["0.1.0"]["status"], "RESEARCH_ONLY")
        latest = mr.find(self.path)
        self.assertEqual([record["module_id"] for record in latest],
                         ["a.module", "b.module"])
        self.assertEqual([record["version"] for record in latest], ["0.2.0", "0.1.0"])
        agri = mr.find(self.path, kind="agri")
        self.assertEqual([record["module_id"] for record in agri], ["b.module"])
        all_versions = mr.find(self.path, latest_only=False)
        self.assertEqual(len(all_versions), 3)

    def test_export_for_api_is_deterministic_and_sorted(self):
        first = mr.export_for_api(self.path)
        second = mr.export_for_api(self.path)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["schema"], mr.EXPORT_SCHEMA)
        self.assertEqual([module["module_id"] for module in first["modules"]],
                         ["a.module", "b.module"])
        self.assertEqual(first["counts"]["modules"], 2)
        self.assertEqual(first["counts"]["by_kind"], {"agri": 1, "market": 1})
        self.assertTrue(all(module["status"] == "RESEARCH_ONLY"
                            for module in first["modules"]))
        for module in first["modules"]:
            self.assertNotIn("status_history", module)

    def test_registry_determinism(self):
        first = Path(self.directory.name) / "first.jsonl"
        second = Path(self.directory.name) / "second.jsonl"
        for target in (first, second):
            for module_id, version, kind, seed in (
                ("x.module", "0.1.0", "core", "x"),
                ("y.module", "1.0.0", "other", "y"),
            ):
                mr.register(target, passport(module_id=module_id, version=version, kind=kind,
                                             code_sha256=mr.sha256_text(seed)))
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_corrupted_log_is_detected(self):
        broken = Path(self.directory.name) / "broken.jsonl"
        broken.write_text("{not json\n", encoding="utf-8")
        report = mr.validate_registry(broken)
        self.assertFalse(report["valid"])
        self.assertIn("not JSON", report["errors"][0])
        with self.assertRaises(mr.ModuleRegistryError):
            mr.load_entries(broken)
        # a missing log is treated as empty (nothing to validate), not as corruption
        empty = mr.validate_registry(self.path / "missing")
        self.assertTrue(empty["valid"])
        self.assertEqual(empty["entries"], 0)


class CodeHash(unittest.TestCase):
    def test_code_hash_is_deterministic_and_order_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.py"
            second = Path(tmp) / "b.py"
            first.write_text("print('a')\n", encoding="utf-8", newline="\n")
            second.write_text("print('b')\n", encoding="utf-8", newline="\n")
            one = mr.code_hash({"a.py": first, "b.py": second})
            two = mr.code_hash({"b.py": second, "a.py": first})
            self.assertEqual(one, two)
            self.assertEqual(len(one["sha256"]), 64)
            first.write_text("print('changed')\n", encoding="utf-8", newline="\n")
            self.assertNotEqual(one, mr.code_hash({"a.py": first, "b.py": second}))
            with self.assertRaises(mr.ModuleRegistryError):
                mr.code_hash({"missing.py": Path(tmp) / "missing.py"})


def resolve_entrypoint(entry: str):
    """Resolve a passport entrypoint: a repository file, a module or a ``module.attribute``."""
    if entry.endswith(".py"):
        path = ROOT / entry
        if not path.is_file():
            raise AssertionError(f"entrypoint file is missing: {entry}")
        return path
    try:
        return importlib.import_module(entry)
    except ImportError:
        module_name, _, attribute = entry.rpartition(".")
        module = importlib.import_module(module_name)
        if not hasattr(module, attribute):
            raise AssertionError(f"entrypoint {entry} has no attribute {attribute!r}")
        return getattr(module, attribute)


def build_registered_module(module_id: str) -> dict:
    """Import and actually build one registered module: identity, status, components, checks."""
    import amfe.agentic
    from amfe import markets, markets_agri, markets_finance
    from amfe.markets import gas, storage  # noqa: F401  (registers gas.lng_eu)

    if module_id in ("oil.brent", "gas.lng_eu"):
        module = markets.get_module(module_id)  # framework registry, keyed by module_id
        result = module.run()
        return {"module_id": result.module_id, "status": result.status,
                "components": [scenario.scenario_id for scenario in module.scenarios()],
                "checks": dict(result.checks)}
    if module_id == "oil.storage_curve":
        curve = storage.build_curve(storage.DEFAULT_PARAMS, spot=100.0, cover_days=30.0)
        components = (["storage_state", "futures_curve"]
                      + [item.episode_id for item in storage.EPISODES] + ["oil_cross_check"])
        return {"module_id": storage.MODULE_ID, "status": curve.status,
                "components": components, "checks": dict(curve.checks)}
    if module_id == "AGRI_SOY":
        passport = markets_agri.module_passport()
        markets_agri.SoyAgriModule()  # the module class is constructible without data
        return {"module_id": passport["module_id"], "status": passport["status"],
                "components": list(passport["components"]), "checks": {}}
    if module_id == "markets.finance":
        cards = markets_finance.model_cards()
        return {"module_id": "markets.finance", "status": "RESEARCH_ONLY",
                "components": [card.to_dict().get("model_id", key)
                               for key, card in cards.items()], "checks": {}}
    if module_id == "agentic.sim":
        demo = amfe.agentic.run_demo()
        checks = {f"{label}.{name}": bool(result.checks[name])
                  for label, result in demo.items()
                  for name in ("double_entry", "financial_conservation",
                               "sfc_saving_equals_investment")}
        return {"module_id": "agentic.sim", "status": "RESEARCH_ONLY", "components": None,
                "checks": checks}
    if module_id == "world.p2":
        from amfe.world import io_core, sfc_core, sfc_io  # noqa: F401  (import is the build gate)
        return {"module_id": "world.p2", "status": "RESEARCH_ONLY", "components": None,
                "checks": {}}
    if module_id == "world.institutions":
        from amfe.world import institutions
        profile = institutions.find_example_profile("EXAMPLE-MID")
        scenarios = institutions.default_scenarios()
        report = institutions.scenario_overlay(profile, scenarios[1])  # REFORM
        modifier = institutions.as_scenario_modifier(report)
        checks = {name: bool(value) for name, value in report["checks"].items()
                  if name not in ("gating", "causal_claim")}
        checks["gating_is_false"] = report["checks"]["gating"] is False
        checks["causal_claim_is_false"] = report["checks"]["causal_claim"] is False
        checks["modifier_gating_is_false"] = modifier["gating"] is False
        checks["score_matches_profile"] = (
            institutions.institutional_score(profile.indicators) == profile.score())
        return {"module_id": "world.institutions", "status": report["status"],
                "components": [scenario.scenario_id for scenario in scenarios],
                "checks": checks}
    if module_id == "world.technology":
        from amfe.world import technology
        described = technology.describe()
        report = technology.build_report(["baseline"], horizon_years=5)
        outcome = technology.run_scenario(technology.SCENARIOS["baseline"],
                                          technology.DEFAULT_PROFILES,
                                          technology.GrowthAssumptions(), 5)
        checks = {name: bool(value) for name, value in report["checks"].items()}
        checks["run_scenario_checks_pass"] = all(outcome["checks"].values())
        checks["code_module_id"] = described["module_id"] == "world.technology_diffusion"
        checks["admission_is_false"] = report["admission"] is False
        return {"module_id": "world.technology", "status": report["status"],
                "components": list(described["scenarios"]), "checks": checks}
    raise AssertionError(f"no build check for {module_id!r}")


class Tool(unittest.TestCase):
    EXPECTED = EXPECTED

    def test_registers_the_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "modules.jsonl"
            report = register_modules.build_report(registry=registry)
            self.assertEqual(report["entries"], len(self.EXPECTED))
            self.assertTrue(report["all_research_only"])
            self.assertTrue(report["validation"]["valid"], report["validation"]["errors"])
            self.assertEqual(report["export_counts"]["by_status"],
                             {"RESEARCH_ONLY": len(self.EXPECTED)})
            self.assertEqual(report["export_counts"]["by_kind"],
                             {"agentic": 1, "agri": 1, "finance": 1, "market": 3, "world": 3})
            found = {item["module_id"]: item for item in report["registered"]}
            self.assertEqual(set(found), set(self.EXPECTED))
            for module_id, kind in self.EXPECTED.items():
                self.assertEqual(found[module_id]["kind"], kind)
                self.assertEqual(found[module_id]["status"], "RESEARCH_ONLY")
                self.assertEqual(len(found[module_id]["code_sha256"]), 64)
            # world P2 0.2 is anchored to the local release manifest
            self.assertEqual(found["world.p2"]["version"], "0.2.0")
            self.assertEqual(found["world.p2"]["data_status"], "LOCAL_ONLY")
            self.assertEqual(len(found["world.p2"]["data_sha256"] or ""), 64)
            # gas is pure assumptions; storage is pinned to the committed EIA series
            self.assertEqual(found["gas.lng_eu"]["data_status"], "ASSUMPTION_BASED")
            self.assertIsNone(found["gas.lng_eu"]["data_sha256"])
            self.assertEqual(found["oil.storage_curve"]["version"], "0.1.0")
            if (ROOT / "data" / "oil" / "brent_monthly_eia.csv").is_file():
                self.assertEqual(found["oil.storage_curve"]["data_status"], "COMMITTED")
                self.assertEqual(len(found["oil.storage_curve"]["data_sha256"] or ""), 64)
            # the export is API-ready and the log validates
            export = mr.export_for_api(registry)
            self.assertEqual(export["counts"]["modules"], len(self.EXPECTED))
            self.assertEqual([module["module_id"] for module in export["modules"]],
                             sorted(self.EXPECTED))

    def test_repeated_run_is_deterministic_and_fails_on_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.jsonl"
            second = Path(tmp) / "second.jsonl"
            first_report = register_modules.build_report(registry=first)
            second_report = register_modules.build_report(registry=second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            first_report.pop("registry")
            second_report.pop("registry")
            self.assertEqual(json.dumps(first_report, sort_keys=True),
                             json.dumps(second_report, sort_keys=True))
            with self.assertRaises(mr.ModuleRegistryError):
                register_modules.build_report(registry=first)
            self.assertEqual(len(mr.load_entries(first)), len(self.EXPECTED))

    def test_the_g2_records_keep_their_order(self):
        """The five G2 records stay first: new modules are appended, the history is not reordered."""
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "modules.jsonl"
            register_modules.build_report(registry=registry)
            ids = [entry["module_id"] for entry in mr.load_entries(registry)]
            self.assertEqual(ids[:5], ["oil.brent", "AGRI_SOY", "markets.finance",
                                       "agentic.sim", "world.p2"])
            self.assertEqual(ids[5:7], ["gas.lng_eu", "oil.storage_curve"])
            self.assertEqual(sorted(ids[7:]),
                             ["world.institutions", "world.technology"])

    def test_cli_fails_closed_on_bad_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = register_modules.main(["--registry", str(Path(tmp) / "modules.jsonl"),
                                          "--date", "bad-date", "--json"])
            self.assertEqual(code, 1)


class BuildsAndPassports(unittest.TestCase):
    """Every registered module imports/builds and its passport matches the code (G5)."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.registry = Path(cls.directory.name) / "modules.jsonl"
        #: importing amfe.markets.gas registers it in the framework registry for the whole process
        #: (self-registration in amfe.markets.contract). The build test needs that, but
        #: forecast_api.build_forecast assumes an oil-shaped summary (price_peak) and crashes on a gas
        #: module in the same process; the registry is therefore restored to the state it had before
        #: this class, so a test file that imported the gas module itself keeps it (see docs/161 §10).
        from amfe.markets import contract
        cls._saved_modules = dict(contract._MODULES)
        cls.report = register_modules.build_report(registry=cls.registry)
        cls.state = mr.state(cls.registry)
        cls.manifests = register_modules._module_passports()
        cls.code_keys = {manifest["module_id"]: name
                         for name, manifest in cls.manifests.items()}

    @classmethod
    def tearDownClass(cls):
        from amfe.markets import contract
        contract._MODULES.clear()
        contract._MODULES.update(cls._saved_modules)
        cls.directory.cleanup()

    def passport(self, module_id: str) -> dict:
        data = self.state[module_id]
        return data["versions"][data["latest_version"]]

    def test_every_module_builds_and_matches_its_passport(self):
        for module_id in EXPECTED:
            with self.subTest(module=module_id):
                built = build_registered_module(module_id)
                passport = self.passport(module_id)
                self.assertEqual(built["module_id"], module_id)
                # the module's own label may be a spelling variant (AGRI_SOY: research_prototype);
                # the registry status is the fixed enum and must stay RESEARCH_ONLY
                self.assertIn(str(built["status"]).upper(),
                              ("RESEARCH_ONLY", "RESEARCH_PROTOTYPE"))
                self.assertEqual(passport["status"], "RESEARCH_ONLY")
                if built["components"] is not None:
                    self.assertEqual(list(passport["components"]), list(built["components"]))
                for name, passed in built["checks"].items():
                    self.assertTrue(passed, f"{module_id}: check {name} failed")

    def test_passport_code_files_entrypoints_and_docs_match_the_repository(self):
        for module_id in EXPECTED:
            with self.subTest(module=module_id):
                passport = self.passport(module_id)
                self.assertEqual(set(passport["code_files"]),
                                 set(register_modules.CODE_FILES[self.code_keys[module_id]]))
                for relative, digest in passport["code_files"].items():
                    path = ROOT / relative
                    self.assertTrue(path.is_file(), relative)
                    self.assertEqual(mr.sha256_file(path), digest, relative)
                recomputed = mr.code_hash({relative: ROOT / relative
                                           for relative in passport["code_files"]})
                self.assertEqual(recomputed["sha256"], passport["code_sha256"])
                for relative in list(passport["docs"]) + list(passport["tests"]):
                    self.assertTrue((ROOT / relative).is_file(), relative)
                for entry in passport["entrypoints"]:
                    resolve_entrypoint(entry)
                self.assertTrue(passport["limitations"])

    def test_g5_passports_are_dated_when_they_were_registered(self):
        """G2 records keep 2026-11-16; gas/storage are dated 2026-11-18, blocks 15/17 2026-11-21."""
        self.assertEqual(self.passport("gas.lng_eu")["created_at"], "2026-11-18")
        self.assertEqual(self.passport("oil.storage_curve")["created_at"], "2026-11-18")
        self.assertEqual(self.passport("world.institutions")["created_at"], "2026-11-21")
        self.assertEqual(self.passport("world.technology")["created_at"], "2026-11-21")
        for module_id in ("oil.brent", "AGRI_SOY", "markets.finance", "agentic.sim", "world.p2"):
            with self.subTest(module=module_id):
                self.assertEqual(self.passport(module_id)["created_at"],
                                 register_modules.DEFAULT_DATE)

    def test_data_pins_anchor_to_the_committed_files(self):
        storage = self.passport("oil.storage_curve")
        series = ROOT / "data" / "oil" / "brent_monthly_eia.csv"
        if series.is_file():
            self.assertEqual(storage["data_sha256"], mr.sha256_file(series))
            self.assertEqual(storage["data_status"], "COMMITTED")
        world = self.passport("world.p2")
        manifest = ROOT / "build" / "p2_release" / "p2_release_0_2.json"
        if manifest.is_file():
            self.assertEqual(world["data_sha256"], mr.sha256_file(manifest))
            self.assertEqual(world["data_status"], "LOCAL_ONLY")

    def test_world_blocks_are_not_market_modules_and_have_no_forecast_target(self):
        """Blocks 15/17 are ``kind=world`` scenario blocks: no MarketModule, no forecast contract."""
        from amfe.markets.contract import MarketError, get_module
        import forecast_api

        for module_id in ("world.institutions", "world.technology"):
            with self.subTest(module=module_id):
                pass_ = self.passport(module_id)
                self.assertEqual(pass_["kind"], "world")
                self.assertEqual(pass_["version"], "0.1.0")
                self.assertEqual(pass_["owner"], "track-b")
                self.assertEqual(pass_["data_status"], "ASSUMPTION_BASED")
                self.assertIsNone(pass_["data_sha256"])
                with self.assertRaises(MarketError):
                    get_module(module_id)
                for entry in pass_["entrypoints"]:
                    resolve_entrypoint(entry)
        market_ids = {str(module.module_id) for module in forecast_api.get_modules()}
        self.assertNotIn("world.institutions", market_ids)
        self.assertNotIn("world.technology", market_ids)

    def test_every_passport_is_research_only_without_promotion_events(self):
        """admission=false: no passport has a status event, criteria or an independent review."""
        self.assertTrue(self.report["all_research_only"])
        for module_id in EXPECTED:
            with self.subTest(module=module_id):
                record = self.passport(module_id)
                self.assertEqual(record["status"], "RESEARCH_ONLY")
                self.assertEqual(record["status_history"], ["RESEARCH_ONLY"])
                self.assertEqual(record["status_events"], 0)
                self.assertIsNone(record.get("criteria"))
                self.assertIsNone(record.get("independent_review"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
