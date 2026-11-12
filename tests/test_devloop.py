"""The pipeline: selection fails open, and the gates say what failed."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from devloop import (  # noqa: E402
    baseline,
    cli,
    config,
    findings,
    gitinfo,
    graph,
    impact,
    process,
    report,
    selector,
    verdict,
)
from devloop.config import CheckSpec  # noqa: E402
from devloop.gitinfo import Change  # noqa: E402

SELECTOR = [sys.executable, "-c"]


def fake_config(root: Path, **overrides) -> config.Config:
    settings = {
        "root": root,
        "python": sys.executable,
        "source_roots": ("pkg",),
        "tests_root": "tests",
        "cache_dir": root / "cache",
        "runner": root / "runner.py",
        "quality": root / "quality.py",
        "selector": (),
        "shared": ("pyproject.toml",),
        "always": (),
        "max_fraction": 0.9,
        "policy": None,
    }
    settings.update(overrides)
    return config.Config(**settings)


def fake_args(**overrides) -> argparse.Namespace:
    values = {
        "command": "check",
        "full": False,
        "base": None,
        "jobs": 0,
        "no_tests": False,
        "files": [],
        "only": [],
        "last_failed": False,
        "no_cache": False,
        "keep_going": False,
        "explain": False,
        "changed_only": False,
        "check": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class SelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self._write("pkg/__init__.py", "")
        self._write("pkg/module.py", "")
        self._write("tests/ui/test_a.py", "")
        self._write("tests/ui/test_b.py", "")
        self._write("tests/ui/test_guard.py", "")
        self._write("tests/ui/support.py", "")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _config(self, selector: list[str]) -> config.Config:
        return config.Config(
            root=self.root,
            python=sys.executable,
            source_roots=("pkg",),
            tests_root="tests",
            cache_dir=self.root / "cache",
            runner=self.root / "runner.py",
            quality=self.root / "quality.py",
            selector=tuple(selector),
            shared=("pyproject.toml",),
            always=("tests/ui/test_guard.py",),
            max_fraction=0.9,
        )

    def _print(self, *paths: str) -> list[str]:
        script = "; ".join(f"print(r'{path}')" for path in paths)
        return [*SELECTOR, script]

    def test_parse_accepts_absolute_and_relative_test_paths(self) -> None:
        cfg = self._config([])
        absolute = (self.root / "tests" / "ui" / "test_a.py").resolve()
        output = f"{absolute}\ntests/ui/test_b.py\nnot a path\ntests/ui/support.py\n"
        self.assertEqual(impact.parse(cfg, output), ["tests/ui/test_a.py", "tests/ui/test_b.py"])

    def test_selector_failure_fails_open_to_the_full_suite(self) -> None:
        cfg = self._config([*SELECTOR, "raise SystemExit(2)"])
        selection = impact.select(cfg, [Change("pkg/module.py", " M")])
        self.assertTrue(selection.fail_open)
        self.assertEqual(set(selection.tests), set(impact.all_tests(cfg)))

    def test_empty_selector_output_fails_open(self) -> None:
        cfg = self._config([*SELECTOR, "pass"])
        selection = impact.select(cfg, [Change("pkg/module.py", " M")])
        self.assertTrue(selection.fail_open)
        self.assertIn("selected no tests", selection.reason)

    def test_always_files_are_added_to_the_selection(self) -> None:
        cfg = self._config(self._print("tests/ui/test_a.py"))
        selection = impact.select(cfg, [Change("pkg/module.py", " M")])
        self.assertFalse(selection.fail_open)
        self.assertEqual(selection.tests, ("tests/ui/test_a.py", "tests/ui/test_guard.py"))

    def test_shared_change_runs_everything(self) -> None:
        cfg = self._config([])
        selection = impact.select(cfg, [Change("pyproject.toml", " M")])
        self.assertTrue(selection.fail_open)
        self.assertEqual(set(selection.tests), set(impact.all_tests(cfg)))

    def test_shared_deletion_runs_everything(self) -> None:
        cfg = self._config([])
        selection = impact.select(cfg, [Change("pyproject.toml", " D")])
        self.assertTrue(selection.fail_open)
        self.assertEqual(set(selection.tests), set(impact.all_tests(cfg)))

    def test_unrelated_change_skips_tests(self) -> None:
        cfg = self._config([])
        selection = impact.select(cfg, [Change("docs/readme.md", "??")])
        self.assertTrue(selection.empty)

    def test_deleted_source_fails_open(self) -> None:
        cfg = self._config([])
        selection = impact.select(cfg, [Change("pkg/module.py", " D")])
        self.assertTrue(selection.fail_open)
        self.assertEqual(set(selection.tests), set(impact.all_tests(cfg)))

    def test_invalid_selector_output_fails_open(self) -> None:
        cfg = self._config(self._print("tests/ui/test_a.py", "not-a-test"))
        selection = impact.select(cfg, [Change("pkg/module.py", " M")])
        self.assertTrue(selection.fail_open)
        self.assertIn("invalid test path", selection.reason)

    def test_builtin_selector_reaches_importing_test(self) -> None:
        self._write("tests/ui/test_a.py", "from pkg import module\n")
        cfg = self._config([])
        selected = selector.select(cfg, [Change("pkg/module.py", " M")])
        self.assertEqual(selected, ["tests/ui/test_a.py"])

    def test_staged_rename_out_of_source_runs_full_suite(self) -> None:
        with patch("devloop.gitinfo._git", return_value="R  docs/module.py\0pkg/module.py\0"):
            changes = gitinfo.working_tree(self.root)
        self.assertIn(Change("pkg/module.py", " D"), changes)
        selection = impact.select(self._config([]), changes)
        self.assertTrue(selection.fail_open)
        self.assertEqual(set(selection.tests), set(impact.all_tests(self._config([]))))

    def test_staged_copy_does_not_delete_original(self) -> None:
        with patch("devloop.gitinfo._git", return_value="C  docs/module.py\0pkg/module.py\0"):
            changes = gitinfo.working_tree(self.root)
        self.assertEqual(changes, [Change("docs/module.py", "C ")])

    def test_committed_deletion_with_other_source_change_runs_full_suite(self) -> None:
        self._write("tests/ui/test_a.py", "from pkg import module\n")
        self._write("tests/ui/test_b.py", "from pkg import deleted\n")

        def git_output(_root: Path, *args: str) -> str:
            if args[0] == "diff":
                return "D\0pkg/deleted.py\0M\0pkg/module.py\0"
            return ""

        with patch("devloop.gitinfo._git", side_effect=git_output):
            changes = gitinfo.since(self.root, "main")
        self.assertIn(Change("pkg/deleted.py", " D"), changes)
        selection = impact.select(self._config(self._print("tests/ui/test_a.py")), changes)
        self.assertTrue(selection.fail_open)
        self.assertEqual(set(selection.tests), set(impact.all_tests(self._config([]))))

    def test_pytest_suffix_pattern_is_discovered(self) -> None:
        self._write("tests/ui/example_test.py", "from pkg import module\n")
        cfg = self._config([])
        self.assertIn("tests/ui/example_test.py", impact.all_tests(cfg))
        self.assertEqual(
            selector.select(cfg, [Change("pkg/module.py", " M")]),
            ["tests/ui/example_test.py"],
        )

    def test_root_conftest_change_runs_full_suite(self) -> None:
        self._write("conftest.py", "")
        cfg = self._config([])
        selection = impact.select(cfg, [Change("conftest.py", " M")])
        self.assertTrue(selection.fail_open)
        self.assertEqual(set(selection.tests), set(impact.all_tests(cfg)))

    def test_root_conftest_deletion_runs_full_suite(self) -> None:
        cfg = self._config([])
        selection = impact.select(cfg, [Change("conftest.py", " D")])
        self.assertTrue(selection.fail_open)
        self.assertEqual(set(selection.tests), set(impact.all_tests(cfg)))


class GraphTest(unittest.TestCase):
    def test_imports_relative_patch_and_importlib_edges(self) -> None:
        source = """
from . import sibling
from .module import name
import os
from unittest.mock import patch

patch("pkg.module.Class.method")
importlib.import_module("pkg.other")
"""
        tree = __import__("ast").parse(source)
        known = {
            "pkg.module": "pkg/module.py",
            "pkg.other": "pkg/other.py",
            "pkg.sibling": "pkg/sibling.py",
        }
        found = graph.extract_imports(tree, "pkg.local", known)
        self.assertIn("pkg.sibling", found)
        self.assertIn("pkg.module", found)
        self.assertIn("os", found)
        self.assertIn("pkg.other", found)

    def test_dependents_follow_transitive_reverse_edges(self) -> None:
        g = graph.ImportGraph(
            imports={
                "pkg.a": {"pkg.b"},
                "pkg.b": {"pkg.c"},
                "tests.ui.test_a": {"pkg.a"},
            },
            tests={"tests.ui.test_a": "tests/ui/test_a.py"},
        )
        graph._rebuild_reverse(g)
        self.assertEqual(g.dependents({"pkg.c"}), {"tests.ui.test_a"})


class ConfigTest(unittest.TestCase):
    def test_find_root_from_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pyproject.toml").write_text(
                '[tool.devloop]\nsource_roots = ["src/pkg"]\n', encoding="utf-8"
            )
            child = root / "tests" / "nested"
            child.mkdir(parents=True)
            # find_root resolves its candidate: a Windows temp path may be a short
            # 8.3 name, and the resolved form is the one the config must carry.
            self.assertEqual(config.find_root(child), root.resolve())
            cfg = config.load(root)
            self.assertEqual(cfg.source_roots, ("src/pkg",))
            self.assertIsNone(cfg.runner)
            self.assertIsNone(cfg.quality)

    def test_src_layout_module_names(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pkg = root / "src" / "pkg"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "module.py").write_text("", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_a.py").write_text("from pkg import module\n", encoding="utf-8")
            cfg = config.Config(
                root,
                sys.executable,
                ("src/pkg",),
                "tests",
                root / ".devloop",
                None,
                None,
                (),
                (),
                (),
                0.9,
            )
            self.assertEqual(graph.source_module(cfg, pkg / "module.py"), "pkg.module")
            self.assertEqual(
                selector.select(cfg, [Change("src/pkg/module.py", " M")]),
                ["tests/test_a.py"],
            )

    def test_single_file_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "src" / "single.py"
            source.parent.mkdir()
            source.write_text("VALUE = 1\n", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_single.py").write_text("import single\n", encoding="utf-8")
            cfg = config.Config(
                root,
                sys.executable,
                ("src/single.py",),
                "tests",
                root / ".devloop",
                None,
                None,
                (),
                (),
                (),
                0.9,
            )
            self.assertEqual(cli._all_python(cfg), ["src/single.py", "tests/test_single.py"])
            self.assertEqual(
                selector.select(cfg, [Change("src/single.py", " M")]),
                ["tests/test_single.py"],
            )

    def test_package_initializer_relative_import_keeps_dependent_test(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pkg = root / "src" / "pkg"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("from . import helper\n", encoding="utf-8")
            (pkg / "helper.py").write_text("", encoding="utf-8")
            (pkg / "other.py").write_text("", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_package.py").write_text("import pkg\n", encoding="utf-8")
            (tests / "test_other.py").write_text("from pkg import other\n", encoding="utf-8")
            cfg = config.Config(
                root,
                sys.executable,
                ("src/pkg",),
                "tests",
                root / ".devloop",
                None,
                None,
                (),
                (),
                (),
                1.0,
            )
            selected = selector.select(
                cfg,
                [Change("src/pkg/helper.py", " M"), Change("src/pkg/other.py", " M")],
            )
            self.assertEqual(selected, ["tests/test_other.py", "tests/test_package.py"])

    def test_source_roots_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root, "pyproject.toml", '[tool.devloop]\ntests_root = "tests"\n')
            with self.assertRaisesRegex(ValueError, "source_roots"):
                config.load(root)

    def test_max_fraction_out_of_range_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            text = '[tool.devloop]\nsource_roots = ["pkg"]\nmax_fraction = 2\n'
            write(root, "pyproject.toml", text)
            with self.assertRaisesRegex(ValueError, "max_fraction"):
                config.load(root)


class GitTest(unittest.TestCase):
    def test_git_command_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "git status.*failed"):
                gitinfo.working_tree(Path(folder))

    def test_working_tree_deletion_overrides_committed_modification(self) -> None:
        def git_output(_root: Path, *args: str) -> str:
            if args[0] == "diff":
                return "M\0pkg/module.py\0"
            return " D pkg/module.py\0"

        with patch("devloop.gitinfo._git", side_effect=git_output):
            changes = gitinfo.since(Path("."), "main")
        self.assertEqual(changes, [Change("pkg/module.py", " D")])


class CliTest(unittest.TestCase):
    def test_select_tests_from_project_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pyproject.toml").write_text(
                '[tool.devloop]\nsource_roots = ["src/pkg"]\n', encoding="utf-8"
            )
            pkg = root / "src" / "pkg"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "module.py").write_text("", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_a.py").write_text("from pkg import module\n", encoding="utf-8")
            output = StringIO()
            with (
                patch("devloop.config.Path.cwd", return_value=tests),
                patch("devloop.gitinfo.collect", return_value=[Change("src/pkg/module.py", " M")]),
                redirect_stdout(output),
            ):
                result = cli.main(["select-tests"])
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue().strip(), "tests/test_a.py")


class RecordingSteps:
    """A ``Steps`` stand-in that records commands instead of running them."""

    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        reported: dict[str, list[findings.Finding]] | None = None,
    ) -> None:
        self.commands: list[tuple[str, list[str]]] = []
        self.failures: list[str] = []
        self.seconds = 0.0
        self.outputs = dict(outputs or {})
        self.reported = dict(reported or {})
        self.verdicts: dict[str, bool] = {}
        self.ran: set[str] = set()

    def run(self, label: str, command: list[str] | None) -> bool:
        self.ran.add(label)
        self.commands.append((label, command or []))
        return True

    def collect(self, label: str) -> list[findings.Finding]:
        return list(self.reported.get(label, []))

    def settle(self, label: str, ok: bool) -> None:
        self.verdicts[label] = ok
        if ok:
            self.failures[:] = [failure for failure in self.failures if failure != label]
        elif label not in self.failures:
            self.failures.append(label)

    def failure_output(self, label: str) -> str:
        return self.outputs.get(label, "")


class FailureReportTest(unittest.TestCase):
    """A failing shard leaves its ids and its log, and ``check`` prints them."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        self.cfg = fake_config(self.root)

    def _summary(self, payload: object) -> None:
        write(self.root, "cache/run-tests.json", json.dumps(payload))

    def test_the_tail_of_the_log_is_printed_with_the_ids(self) -> None:
        self._summary(
            {
                "shards": [
                    {
                        "name": "test_x.py",
                        "failed": 1,
                        "failed_tests": ["tests/x.py::T::a"],
                        "log": "cache/shard-test_x.py.log",
                    }
                ]
            }
        )
        write(self.root, "cache/shard-test_x.py.log", "line one\nE   boom\n1 failed\n")
        output = StringIO()
        with redirect_stdout(output):
            report.test_failures(self.cfg)
        text = output.getvalue()
        self.assertIn("failing: tests/x.py::T::a", text)
        self.assertIn("E   boom", text)

    def test_a_missing_summary_prints_nothing(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            report.test_failures(self.cfg)
        self.assertEqual(output.getvalue(), "")

    def test_a_corrupt_summary_reads_as_empty(self) -> None:
        write(self.root, "cache/run-tests.json", "{not json")
        self.assertEqual(report.summary(self.cfg), {})


class PolicyStepTest(unittest.TestCase):
    """The policy gate covers the whole tree, so it receives no paths."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_policy_runs_without_paths_even_when_nothing_changed(self) -> None:
        policy = self.root / "policy.py"
        steps = RecordingSteps()
        cli._static_steps(fake_config(self.root, policy=policy), steps, [], full=False)
        self.assertIn(("policy", [sys.executable, str(policy)]), steps.commands)

    def test_an_unconfigured_policy_is_skipped(self) -> None:
        steps = RecordingSteps()
        cli._static_steps(fake_config(self.root), steps, [], full=False)
        self.assertNotIn("policy", [label for label, _ in steps.commands])

    def test_quality_is_skipped_when_no_python_changed(self) -> None:
        steps = RecordingSteps()
        cli._static_steps(fake_config(self.root), steps, [], full=False)
        self.assertNotIn("quality", [label for label, _ in steps.commands])


class FullStaticStepsTest(unittest.TestCase):
    """``--full`` gates every Python file, not only the changed ones."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "pkg/module.py", "")
        write(self.root, "tests/test_a.py", "")

    def _commands(self, files: list[str], full: bool) -> dict[str, list[str]]:
        steps = RecordingSteps()
        cli._static_steps(fake_config(self.root), steps, files, full=full)
        return dict(steps.commands)

    def test_full_mode_ruffs_every_python_file(self) -> None:
        commands = self._commands([], full=True)
        self.assertIn("pkg/module.py", commands["ruff"])
        self.assertIn("tests/test_a.py", commands["ruff"])
        self.assertIn("pkg/module.py", commands["format"])

    def test_changed_mode_ruffs_only_the_changed_files(self) -> None:
        commands = self._commands(["pkg/module.py"], full=False)
        self.assertIn("pkg/module.py", commands["ruff"])
        self.assertNotIn("tests/test_a.py", commands["ruff"])


class VerifySelectionTest(unittest.TestCase):
    """Explicit selections bypass the selector and the full-run cache."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.cfg = fake_config(Path(self._temp.name).resolve())

    def test_explicit_files_are_forwarded_to_the_runner(self) -> None:
        command = cli._test_command(self.cfg, [], fake_args(files=["tests/ui/test_a.py"]))
        self.assertIsNotNone(command)
        self.assertIn(str(self.cfg.runner), command)
        self.assertEqual(command[-2:], ["--files", "tests/ui/test_a.py"])

    def test_only_last_failed_and_jobs_are_forwarded(self) -> None:
        args = fake_args(only=["ui"], last_failed=True, jobs=3)
        command = cli._test_command(self.cfg, [], args)
        self.assertIsNotNone(command)
        self.assertIn("--only", command)
        self.assertIn("--last-failed", command)
        self.assertEqual(command[-2:], ["--jobs", "3"])

    def test_no_tests_skips_the_step(self) -> None:
        self.assertIsNone(cli._test_command(self.cfg, [], fake_args(no_tests=True)))

    def test_only_a_plain_full_run_is_cacheable(self) -> None:
        with patch("devloop.verdict.tree_key", return_value="tree-key"):
            self.assertIsNone(cli._cache_key(self.cfg, fake_args(full=False)))
            self.assertIsNone(cli._cache_key(self.cfg, fake_args(full=True, no_cache=True)))
            files = fake_args(full=True, files=["tests/ui/test_a.py"])
            self.assertIsNone(cli._cache_key(self.cfg, files))
            self.assertEqual(cli._cache_key(self.cfg, fake_args(full=True)), "tree-key")


class VerdictCacheTest(unittest.TestCase):
    """A green full run is replayed only for the exact tree it gated."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.cfg = fake_config(Path(self._temp.name).resolve())

    def test_a_recorded_key_is_recent_and_another_is_not(self) -> None:
        verdict.remember(self.cfg, "abc")
        self.assertTrue(verdict.recent(self.cfg, "abc"))
        self.assertFalse(verdict.recent(self.cfg, "def"))

    def test_a_corrupt_cache_reads_as_no_verdict(self) -> None:
        write(self.cfg.cache_dir, "verify-full.json", "{not json")
        self.assertFalse(verdict.recent(self.cfg, "abc"))

    def test_tree_key_is_stable_until_the_tree_changes(self) -> None:
        root = Path(self._temp.name).resolve()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        cfg = fake_config(root)
        first = verdict.tree_key(cfg)
        self.assertEqual(verdict.tree_key(cfg), first)
        write(root, "pkg/module.py", "x = 1\n")
        self.assertNotEqual(verdict.tree_key(cfg), first)


class DirectPytestTest(unittest.TestCase):
    """A project without a runner still gets a working test step."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "tests/ui/test_a.py", "")
        write(self.root, "tests/ui/test_b.py", "")
        self.cfg = fake_config(self.root, runner=None)

    def test_the_selected_files_are_passed_positionally(self) -> None:
        args = fake_args(files=["tests/ui/test_a.py"])
        command = cli._test_command(self.cfg, [], args)
        self.assertEqual(command[:4], [sys.executable, "-m", "pytest", "-q"])
        self.assertEqual(command[4:], ["tests/ui/test_a.py"])

    def test_only_filters_the_discovered_test_files(self) -> None:
        command = cli._test_command(self.cfg, [], fake_args(only=["ui/"]))
        self.assertEqual(command[-2:], ["tests/ui/test_a.py", "tests/ui/test_b.py"])

    def test_last_failed_replays_with_lf(self) -> None:
        command = cli._test_command(self.cfg, [], fake_args(last_failed=True))
        self.assertEqual(command[-1], "--lf")

    def test_an_only_token_matching_nothing_is_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "no test file matches"):
            cli._test_command(self.cfg, [], fake_args(only=["nowhere"]))

    def test_jobs_is_ignored_without_a_runner(self) -> None:
        args = fake_args(files=["tests/ui/test_a.py"], jobs=4)
        self.assertNotIn("--jobs", cli._test_command(self.cfg, [], args))


class GitOutputTest(unittest.TestCase):
    def test_output_is_empty_when_git_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(gitinfo.output(Path(folder), "status"), "")


class FindingsParsingTest(unittest.TestCase):
    """Each tool's output becomes stable ``path:code`` keys."""

    def setUp(self) -> None:
        self.root = Path(tempfile.gettempdir()).resolve()

    def test_ruff_json_is_keyed_by_path_and_code(self) -> None:
        output = json.dumps(
            [
                {
                    "code": "E501",
                    "filename": str(self.root / "pkg" / "module.py"),
                    "location": {"row": 12},
                    "message": "Line too long",
                },
                {"code": None, "filename": "pkg/module.py", "location": {"row": 3}, "message": "?"},
            ]
        )
        found = findings.parse_ruff(output, self.root)
        self.assertEqual([item.key for item in found], ["pkg/module.py:E501", "pkg/module.py:misc"])
        self.assertIn("pkg/module.py:12", found[0].message)

    def test_ruff_output_before_the_array_is_ignored(self) -> None:
        output = 'warning: something\n[{"code": "F401", "filename": "pkg/x.py"}]'
        self.assertEqual(
            [item.key for item in findings.parse_ruff(output, self.root)], ["pkg/x.py:F401"]
        )

    def test_ruff_output_that_is_not_json_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not print JSON"):
            findings.parse_ruff("boom", self.root)

    def test_mypy_errors_are_keyed_by_path_and_code(self) -> None:
        output = (
            "pkg/x.py:4: error: Need type annotation  [var-annotated]\n"
            "pkg/x.py:5: error: no code here\n"
            "pkg/x.py:6: note: a note is not a finding\n"
            "Success: no issues found in 1 source file\n"
        )
        found = findings.parse_mypy(output, self.root)
        self.assertEqual([item.key for item in found], ["pkg/x.py:var-annotated", "pkg/x.py:misc"])

    def test_the_findings_report_needs_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "quality.jsonl"
            path.write_text('{"key": "dupe-main@a.py"}\n{"message": "no key"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "has no key"):
                findings.read_report(path, "quality")

    def test_a_missing_report_is_empty(self) -> None:
        self.assertEqual(findings.read_report(Path("nope.jsonl"), "quality"), [])


class BaselineFileTest(unittest.TestCase):
    """The file is sorted, versioned, and an unreadable one is an error."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        self.path = self.root / "devloop-baseline.json"

    def test_a_round_trip_keeps_sorted_sets(self) -> None:
        baseline.save(self.path, {"ruff": {"b.py:E501", "a.py:F401"}, "mypy": set()})
        self.assertEqual(baseline.load(self.path), {"ruff": {"a.py:F401", "b.py:E501"}})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn('"version": 1', text)
        self.assertLess(text.index("a.py:F401"), text.index("b.py:E501"))

    def test_a_corrupt_baseline_is_an_error(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "is not JSON"):
            baseline.load(self.path)

    def test_a_wrong_version_is_an_error(self) -> None:
        self.path.write_text('{"version": 2, "entries": {}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "version 1"):
            baseline.load(self.path)

    def test_an_empty_baseline_can_be_absent(self) -> None:
        cfg = fake_config(self.root, baseline=None)
        self.assertIsNone(cli._baseline_entries(cfg))

    def test_a_missing_file_turns_the_baseline_off(self) -> None:
        cfg = fake_config(self.root, baseline=self.path)
        self.assertIsNone(cli._baseline_entries(cfg))


class BaselineCompareTest(unittest.TestCase):
    """New findings fail, stale entries are a full-run warning."""

    def _find(self, step: str, key: str) -> findings.Finding:
        return findings.Finding(step, key, key)

    def test_a_new_key_is_new(self) -> None:
        result = baseline.compare(
            {"ruff": {"a.py:E501"}},
            {"ruff": [self._find("ruff", "b.py:E501")]},
            ["ruff"],
            full=False,
        )
        self.assertEqual([item.key for item in result.new["ruff"]], ["b.py:E501"])
        self.assertTrue(result.has_new)

    def test_a_known_key_is_accepted_and_counted(self) -> None:
        result = baseline.compare(
            {"ruff": {"a.py:E501"}},
            {"ruff": [self._find("ruff", "a.py:E501")]},
            ["ruff"],
            full=False,
        )
        self.assertFalse(result.has_new)
        self.assertEqual(result.known, 1)

    def test_stale_entries_are_reported_only_on_a_full_run(self) -> None:
        narrow = baseline.compare({"ruff": {"a.py:E501"}}, {}, ["ruff"], full=False)
        self.assertEqual(narrow.stale, {})
        full = baseline.compare({"ruff": {"a.py:E501"}}, {}, ["ruff"], full=True)
        self.assertEqual(full.stale["ruff"], ("a.py:E501",))

    def test_a_step_that_did_not_run_is_not_stale(self) -> None:
        result = baseline.compare({"mypy": {"x.py:misc"}}, {}, ["ruff"], full=True)
        self.assertEqual(result.stale, {})


class BaselineStepTest(unittest.TestCase):
    """With a baseline the steps are judged by keys, not by an exit code."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "pkg/module.py", "")
        write(self.root, "tests/test_a.py", "")

    def _ruff_output(self, code: str) -> str:
        payload = [
            {"code": code, "filename": "pkg/module.py", "location": {"row": 2}, "message": "x"}
        ]
        return json.dumps(payload)

    def test_ruff_runs_as_json_and_a_known_code_passes(self) -> None:
        steps = RecordingSteps(outputs={"ruff": self._ruff_output("E501")})
        cfg = fake_config(self.root)
        cli._static_steps(
            cfg, steps, ["pkg/module.py"], full=False, entries={"ruff": {"pkg/module.py:E501"}}
        )
        self.assertIn("--output-format", steps.commands[0][1])
        self.assertTrue(steps.verdicts["ruff"])
        self.assertNotIn("ruff", steps.failures)

    def test_a_new_code_fails_the_ruff_step(self) -> None:
        steps = RecordingSteps(outputs={"ruff": self._ruff_output("E501")})
        cfg = fake_config(self.root)
        cli._static_steps(cfg, steps, ["pkg/module.py"], full=False, entries={"ruff": set()})
        self.assertFalse(steps.verdicts["ruff"])
        self.assertIn("ruff", steps.failures)

    def test_a_quality_script_is_judged_by_its_report(self) -> None:
        known = findings.Finding("quality", "dupe-main@pkg/module.py", "dupe-main")
        steps = RecordingSteps(reported={"quality": [known]})
        steps.failures.append("quality")
        cfg = fake_config(self.root)
        cli._static_steps(
            cfg,
            steps,
            ["pkg/module.py"],
            full=False,
            entries={"quality": {"dupe-main@pkg/module.py"}},
        )
        self.assertTrue(steps.verdicts["quality"])
        self.assertNotIn("quality", steps.failures)


class MypyScopeTest(unittest.TestCase):
    """The mypy step follows the configured scope and the changed-only switch."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_the_scope_is_passed_positionally(self) -> None:
        cfg = fake_config(self.root, mypy_scope=("pkg", "tools"))
        command = cli._mypy_command(cfg, [], changed_only=False, baseline_mode=False)
        self.assertEqual(command, [sys.executable, "-m", "mypy", "pkg", "tools"])

    def test_changed_only_keeps_the_changed_files_inside_the_scope(self) -> None:
        cfg = fake_config(self.root, mypy_scope=("pkg",))
        command = cli._mypy_command(
            cfg, ["pkg/a.py", "tests/test_a.py"], changed_only=True, baseline_mode=False
        )
        self.assertEqual(command, [sys.executable, "-m", "mypy", "pkg/a.py"])

    def test_changed_only_with_nothing_in_scope_skips_the_step(self) -> None:
        cfg = fake_config(self.root, mypy_scope=("pkg",))
        self.assertIsNone(
            cli._mypy_command(cfg, ["tests/test_a.py"], changed_only=True, baseline_mode=False)
        )

    def test_the_baseline_mode_asks_for_codes_and_no_summary(self) -> None:
        cfg = fake_config(self.root)
        command = cli._mypy_command(cfg, [], changed_only=False, baseline_mode=True)
        self.assertIn("--show-error-codes", command)
        self.assertIn("--no-error-summary", command)


class StepEnvTest(unittest.TestCase):
    """Every step subprocess sees the configured environment."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_a_configured_variable_reaches_the_step(self) -> None:
        cfg = fake_config(self.root, env=(("QT_QPA_PLATFORM", "offscreen"),))
        steps = cli.Steps(cfg, keep_going=False)
        script = "import os; print(os.environ.get('QT_QPA_PLATFORM', 'missing'))"
        steps.run("probe", [sys.executable, "-c", script])
        self.assertIn("offscreen", steps.outputs["probe"])

    def test_the_findings_file_reaches_the_step_and_is_collected(self) -> None:
        cfg = fake_config(self.root)
        steps = cli.Steps(cfg, keep_going=False)
        script = (
            "import os, pathlib;"
            "pathlib.Path(os.environ['DEVLOOP_FINDINGS_FILE']).write_text("
            "'{\"key\": \"dupe@pkg/x.py\"}\\n', encoding='utf-8')"
        )
        steps.run("quality", [sys.executable, "-c", script])
        self.assertEqual([item.key for item in steps.collect("quality")], ["dupe@pkg/x.py"])


class ExtraCheckTest(unittest.TestCase):
    """``[[tool.devloop.check]]`` runs a project command as a pipeline step."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "pkg/module.py", "")
        write(self.root, "tests/test_a.py", "")

    def _config(self, paths: str) -> config.Config:
        spec = CheckSpec("plan-run", ("{python}", "scripts/plan_run.py"), paths)
        return fake_config(self.root, checks=(spec,))

    def test_a_none_check_gets_no_paths(self) -> None:
        steps = RecordingSteps()
        cli._static_steps(self._config("none"), steps, [], full=False)
        labels = [label for label, _ in steps.commands]
        self.assertIn("plan-run", labels)
        self.assertEqual(dict(steps.commands)["plan-run"][1:], ["scripts/plan_run.py"])

    def test_an_all_check_gets_the_whole_tree(self) -> None:
        steps = RecordingSteps()
        cli._static_steps(self._config("all"), steps, [], full=False)
        command = dict(steps.commands)["plan-run"]
        self.assertIn("pkg/module.py", command)
        self.assertIn("tests/test_a.py", command)

    def test_a_changed_check_is_skipped_without_changes(self) -> None:
        steps = RecordingSteps()
        cli._static_steps(self._config("changed"), steps, [], full=False)
        self.assertNotIn("plan-run", [label for label, _ in steps.commands])

    def test_a_failing_check_fails_the_pipeline(self) -> None:
        steps = RecordingSteps()
        steps.failures.append("plan-run")
        cli._static_steps(self._config("none"), steps, [], full=False)
        self.assertIn("plan-run", steps.failures)


class ScriptedSteps(cli.Steps):
    """A ``Steps`` that answers with canned output instead of running tools."""

    ruff_output = "[]"

    def __init__(self, cfg: config.Config, keep_going: bool, quiet: frozenset[str] = frozenset()):
        super().__init__(cfg, keep_going, quiet)
        self.ran = set()

    def run(self, label: str, command: list[str] | None) -> bool:
        self.ran.add(label)
        self.outputs[label] = self.ruff_output if label == "ruff" else ""
        return True


class BaselineCommandTest(unittest.TestCase):
    """``devloop baseline`` writes the frozen findings and refuses on a broken tool."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "pkg/module.py", "")
        self.path = self.root / "devloop-baseline.json"
        self.cfg = fake_config(self.root, baseline=self.path)
        self.args = fake_args()

    def _run(self) -> int:
        with (
            patch("devloop.cli.config.load", return_value=self.cfg),
            patch("devloop.cli.Steps", ScriptedSteps),
        ):
            return cli.run_baseline(self.args)

    def test_the_observed_findings_are_written(self) -> None:
        ScriptedSteps.ruff_output = json.dumps(
            [{"code": "E501", "filename": "pkg/module.py", "location": {"row": 1}, "message": "x"}]
        )
        self.assertEqual(self._run(), 0)
        self.assertEqual(baseline.load(self.path), {"ruff": {"pkg/module.py:E501"}})

    def test_a_failing_tool_writes_nothing(self) -> None:
        class Broken(ScriptedSteps):
            def run(self, label: str, command: list[str] | None) -> bool:
                self.failures.append(label)
                self.outputs[label] = "traceback"
                return False

        ScriptedSteps.ruff_output = "[]"
        with (
            patch("devloop.cli.config.load", return_value=self.cfg),
            patch("devloop.cli.Steps", Broken),
        ):
            self.assertEqual(cli.run_baseline(self.args), 2)
        self.assertFalse(self.path.exists())


class RunnerExampleTest(unittest.TestCase):
    """The example runner honours the adapter contract on a real project."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "pyproject.toml", '[tool.devloop]\nsource_roots = ["pkg"]\n')
        write(self.root, "pkg/__init__.py", "")
        write(self.root, "tests/test_ok.py", "def test_ok():\n    assert True\n")
        write(self.root, "tests/test_bad.py", "def test_bad():\n    assert False\n")

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        example = Path(__file__).resolve().parents[1] / "examples" / "runner" / "run_tests.py"
        return subprocess.run(
            [sys.executable, str(example), *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
        )

    def test_a_failing_shard_is_recorded_and_reported(self) -> None:
        result = self._run("--files", "tests/test_ok.py", "tests/test_bad.py")
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(
            (self.root / ".devloop" / "run-tests.json").read_text(encoding="utf-8")
        )
        names = [shard["name"] for shard in summary["shards"]]
        self.assertEqual(names, ["test_ok.py", "test_bad.py"])
        failed = [shard for shard in summary["shards"] if shard["failed"]]
        self.assertEqual(failed[0]["failed_tests"], ["tests/test_bad.py::test_bad"])
        self.assertTrue((self.root / failed[0]["log"]).is_file())

    def test_last_failed_replays_only_the_failures(self) -> None:
        self._run("--files", "tests/test_ok.py", "tests/test_bad.py")
        result = self._run("--last-failed")
        self.assertEqual(result.returncode, 1)
        summary = json.loads(
            (self.root / ".devloop" / "run-tests.json").read_text(encoding="utf-8")
        )
        self.assertEqual([shard["name"] for shard in summary["shards"]], ["last-failed"])


class MypyConfigWireTest(unittest.TestCase):
    """``mypy.changed_only`` reaches the pipeline; ``--full`` keeps the whole scope."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "pkg/module.py", "")
        write(self.root, "tests/test_a.py", "")
        self.cfg = fake_config(self.root, mypy_scope=("pkg",), mypy_changed_only=True)

    def _steps(self, args: argparse.Namespace) -> RecordingSteps:
        steps = RecordingSteps()
        with (
            patch("devloop.cli.config.load", return_value=self.cfg),
            patch(
                "devloop.cli.gitinfo.collect",
                return_value=[Change("pkg/module.py", " M")],
            ),
            patch("devloop.cli.Steps", return_value=steps),
            patch("devloop.cli._test_step"),
            patch("devloop.cli.verdict.tree_key", return_value="key"),
            patch("devloop.cli.verdict.recent", return_value=False),
            redirect_stdout(StringIO()),
        ):
            cli.run_check(args)
        return steps

    def test_the_configuration_key_is_honoured_in_changed_mode(self) -> None:
        steps = self._steps(fake_args())
        self.assertEqual(dict(steps.commands)["mypy"][-1], "pkg/module.py")

    def test_a_full_run_ignores_it_and_checks_the_scope(self) -> None:
        steps = self._steps(fake_args(full=True))
        self.assertEqual(dict(steps.commands)["mypy"][-1], "pkg")


class StaleFindingsTest(unittest.TestCase):
    """A step that did not run must not read the previous run's findings file."""

    def test_collect_is_empty_for_a_step_that_never_ran(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg = fake_config(root)
            report_file = cfg.cache_dir / "findings" / "quality.jsonl"
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.write_text('{"key": "old@pkg/x.py"}\n', encoding="utf-8")
            steps = cli.Steps(cfg, keep_going=False)
            steps.failures.append("ruff")
            steps.run("quality", [sys.executable, "-c", "print('never')"])
            self.assertEqual(steps.collect("quality"), [])
            self.assertNotIn("quality", steps.outputs)


class FormatBaselineTest(unittest.TestCase):
    """The formatter is a finding step: ``<path>:format`` freezes into the baseline."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "pkg/module.py", "")
        write(self.root, "tests/test_a.py", "")

    def _steps(self, entries: dict[str, set[str]]) -> RecordingSteps:
        output = "Would reformat: pkg/module.py\n1 file would be reformatted\n"
        steps = RecordingSteps(outputs={"format": output})
        cli._static_steps(
            fake_config(self.root), steps, ["pkg/module.py"], full=False, entries=entries
        )
        return steps

    def test_a_frozen_unformatted_file_passes(self) -> None:
        steps = self._steps({"format": {"pkg/module.py:format"}})
        self.assertTrue(steps.verdicts["format"])

    def test_an_unformatted_file_outside_the_baseline_fails(self) -> None:
        steps = self._steps({"format": set()})
        self.assertFalse(steps.verdicts["format"])
        self.assertIn("format", steps.failures)


class BaselineCheckTest(unittest.TestCase):
    """``baseline --check`` fails on a new finding and on a stale entry."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        write(self.root, "pkg/module.py", "")
        self.path = self.root / "devloop-baseline.json"
        self.cfg = fake_config(self.root, baseline=self.path)

    def _run(self, output: str) -> int:
        ScriptedSteps.ruff_output = output
        with (
            patch("devloop.cli.config.load", return_value=self.cfg),
            patch("devloop.cli.Steps", ScriptedSteps),
            redirect_stdout(StringIO()),
        ):
            return cli.run_baseline(fake_args(check=True))

    def test_a_current_baseline_passes(self) -> None:
        baseline.save(self.path, {})
        self.assertEqual(self._run("[]"), 0)

    def test_a_new_finding_fails(self) -> None:
        baseline.save(self.path, {})
        output = json.dumps([{"code": "E501", "filename": "pkg/module.py"}])
        self.assertEqual(self._run(output), 1)

    def test_a_stale_entry_fails(self) -> None:
        baseline.save(self.path, {"ruff": {"gone.py:F401"}})
        self.assertEqual(self._run("[]"), 1)

    def test_a_missing_baseline_is_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "run devloop baseline first"):
            self._run("[]")


class FindingsOutputTest(unittest.TestCase):
    """A project's own finding prints its key; the built-in tools keep the body."""

    def _print(self, detail: bool) -> str:
        buffer = StringIO()
        finding = findings.Finding("quality", "dupe@pkg/x.py", "two main() definitions")
        with redirect_stdout(buffer):
            report.new_findings("quality", [finding], 0, detail=detail)
        return buffer.getvalue()

    def test_the_default_is_the_key_only(self) -> None:
        output = self._print(detail=False)
        self.assertIn("dupe@pkg/x.py", output)
        self.assertNotIn("two main", output)

    def test_detail_adds_the_message(self) -> None:
        self.assertIn("two main", self._print(detail=True))


class ProcessDecodingTest(unittest.TestCase):
    """A child that prints UTF-8 must not lose its output on a legacy code page."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()

    def test_utf8_output_survives_a_legacy_code_page(self) -> None:
        steps = cli.Steps(fake_config(self.root), keep_going=False)
        script = "import sys; sys.stdout.buffer.write('Иван, Пётр и Ёж'.encode('utf-8'))"
        self.assertTrue(steps.run("probe", [sys.executable, "-c", script]))
        self.assertIn("Иван", steps.outputs["probe"])
        self.assertIn("Ёж", steps.outputs["probe"])

    def test_the_locale_code_page_is_the_second_choice(self) -> None:
        with patch("devloop.process.locale.getpreferredencoding", return_value="cp1251"):
            self.assertEqual(process.decode("Привет".encode("cp1251")), "Привет")

    def test_undecodable_bytes_do_not_raise(self) -> None:
        with patch("devloop.process.locale.getpreferredencoding", return_value="cp1251"):
            text = process.decode(b"\xff\xfe\x98")
        self.assertIsInstance(text, str)


if __name__ == "__main__":
    unittest.main()
