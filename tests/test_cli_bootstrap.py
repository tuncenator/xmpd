"""CLI imports stay in the caller; direct execution can select the repo venv."""

import importlib.machinery
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.parametrize("script_name", ["xmpctl", "xmpd-status"])
def test_import_does_not_replace_the_calling_interpreter(script_name):
    path = Path(__file__).resolve().parents[1] / "bin" / script_name
    loader = importlib.machinery.SourceFileLoader("bootstrap_import_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    with (
        patch("sys.executable", "/another/environment/bin/python"),
        patch("os.execv", side_effect=AssertionError("Import must not re-exec")) as reexec,
    ):
        loader.exec_module(module)
    reexec.assert_not_called()


@pytest.mark.parametrize("script_name", ["xmpctl", "xmpd-status"])
def test_direct_execution_selects_the_repo_interpreter(script_name, tmp_path):
    source = Path(__file__).resolve().parents[1] / "bin" / script_name
    script = tmp_path / "bin" / script_name
    script.parent.mkdir()
    shutil.copyfile(source, script)
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text('#!/bin/sh\nprintf "BOOTSTRAP:%s\\n" "$@"\n')
    interpreter.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines() == [f"BOOTSTRAP:{script}", "BOOTSTRAP:--help"]
