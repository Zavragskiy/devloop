"""The selection wrapper fails open and only ever adds tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from devloop import cli, config, gitinfo, graph, impact, selector  # noqa: E402
from devloop.gitinfo import Change  # noqa: E402

SELECTOR = [sys.executable, "-c"]


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
            self.assertEqual(config.find_root(child), root)
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


if __name__ == "__main__":
    unittest.main()
