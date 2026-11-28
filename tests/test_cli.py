"""Run the `evalanche` command against the fixture targets.

Every test runs the pinned evaluators, so `evalanche` and the
`EVALANCHE_*` variables come from `nix develop` or the flake check.
"""

import json
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODULES = FIXTURES / "modules"
TARGET_FILE = FIXTURES / "targets.nix"
BAD_TARGET_FILE = FIXTURES / "bad-targets.nix"
FLAKE = FIXTURES / "flake"
FLAKE_NIXPKGS = FIXTURES / "flake-nixpkgs"
SCHEMA = FIXTURES.parent / "schema"

NOT_IMPLEMENTED = "evalanche: not implemented: `fuzz` stops after the baseline"


def evalanche(*args: str, cwd: Path = FIXTURES) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["evalanche", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def assert_baseline_passed(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 2, result.stderr
    assert "baseline evaluation passed" in result.stderr
    assert "cppnix: evaluated" in result.stderr
    assert "lix: evaluated" in result.stderr
    assert NOT_IMPLEMENTED in result.stderr


def test_version():
    result = evalanche("--version")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("evalanche ")
    assert lines[1].startswith("cppnix: nix (Nix) ")
    assert lines[2].startswith("lix: nix (Lix, like Nix) ")


def test_no_command_is_a_usage_error():
    result = evalanche()
    assert result.returncode == 2
    assert "usage error: no command given" in result.stderr


def test_unknown_flag_is_a_usage_error():
    result = evalanche("fuzz", "--no-such-flag", "x.nix")
    assert result.returncode == 2


def test_shared_flags_are_accepted():
    result = evalanche(
        "fuzz",
        "--time-budget",
        "10",
        "--max-examples",
        "5",
        "--jobs",
        "2",
        "--json",
        "--property",
        "valid-input",
        "--property",
        "merge.associative",
        "modules/port.nix",
    )
    assert_baseline_passed(result)


def test_unknown_property_is_a_usage_error():
    result = evalanche("fuzz", "--property", "nope", "modules/port.nix")
    assert result.returncode == 2
    assert "unknown property `nope`" in result.stderr


def test_nix_bin_override(tmp_path):
    result = evalanche("fuzz", "--nix-bin", f"lix={tmp_path}", "modules/port.nix")
    assert result.returncode == 2
    assert "lix: exit" not in result.stderr
    assert str(tmp_path / "nix-instantiate") in result.stderr


def test_nix_bin_needs_a_known_name():
    result = evalanche("fuzz", "--nix-bin", "guix=/bin", "modules/port.nix")
    assert result.returncode == 2
    assert "expected NAME=DIR" in result.stderr


# Path shorthand.


def test_shorthand_single_module():
    result = evalanche("fuzz", "modules/port.nix")
    assert_baseline_passed(result)
    assert "target modules/port.nix: kind modules, lib pinned" in result.stderr


def test_shorthand_several_modules_form_one_target():
    result = evalanche("fuzz", "modules/port.nix", "modules/name.nix")
    assert_baseline_passed(result)
    assert "target modules/port.nix modules/name.nix:" in result.stderr


def test_shorthand_module_order_is_kept():
    # `name.nix` reads `config.port`, which only `port.nix` declares, so the
    # target evaluates in either order. The name lists the paths as given.
    result = evalanche("fuzz", "modules/name.nix", "modules/port.nix")
    assert_baseline_passed(result)
    assert "target modules/name.nix modules/port.nix:" in result.stderr


def test_shorthand_missing_path():
    result = evalanche("fuzz", "modules/absent.nix")
    assert result.returncode == 2
    assert "target error: module `modules/absent.nix` does not exist" in result.stderr


def test_shorthand_module_needing_another_argument():
    result = evalanche("fuzz", "modules/needs-pkgs.nix")
    assert result.returncode == 2
    assert "baseline evaluation of `modules/needs-pkgs.nix` failed" in result.stderr
    for evaluator in ("cppnix", "lix"):
        assert f"{evaluator}: exit 1" in result.stderr
    assert "needs the argument `pkgs`" in result.stderr
    assert "sets `specialArgs.pkgs`" in result.stderr


def test_shorthand_baseline_failure():
    result = evalanche("fuzz", "modules/broken.nix")
    assert result.returncode == 2
    assert "baseline evaluation of `modules/broken.nix` failed" in result.stderr
    assert "cppnix: exit 1" in result.stderr
    assert "lix: exit 1" in result.stderr
    assert "is not of type" in result.stderr


# Target files.


def test_file_target():
    result = evalanche("fuzz", "-f", str(TARGET_FILE), "service")
    assert_baseline_passed(result)
    assert "target service: kind modules, lib pinned" in result.stderr


def test_file_target_with_special_args():
    result = evalanche("fuzz", "-f", str(TARGET_FILE), "greeting")
    assert_baseline_passed(result)


def test_file_baseline_failure():
    result = evalanche("fuzz", "-f", str(TARGET_FILE), "broken")
    assert result.returncode == 2
    assert "target error: baseline evaluation of `broken` failed" in result.stderr


def test_file_unknown_target_name():
    result = evalanche("fuzz", "-f", str(TARGET_FILE), "absent")
    assert result.returncode == 2
    assert "has no target `absent`. Available: broken, greeting, service" in (
        result.stderr
    )


def test_file_takes_one_name():
    result = evalanche("fuzz", "-f", str(TARGET_FILE), "service", "greeting")
    assert result.returncode == 2
    assert "usage error: `-f FILE` takes exactly one target name" in result.stderr


def test_unknown_attribute_is_a_target_error():
    result = evalanche("fuzz", "-f", str(BAD_TARGET_FILE), "prefixed")
    assert result.returncode == 2
    assert "target error: target `prefixed`: unknown attribute `prefix`" in (
        result.stderr
    )


def test_unknown_kind_is_a_target_error():
    result = evalanche("fuzz", "-f", str(BAD_TARGET_FILE), "configuration")
    assert result.returncode == 2
    assert (
        'target `configuration`: attribute `kind` has the unknown value "configuration"'
        in result.stderr
    )


def test_missing_modules_is_a_target_error():
    result = evalanche("fuzz", "-f", str(BAD_TARGET_FILE), "noModules")
    assert result.returncode == 2
    assert "target `noModules`: missing attribute `modules`" in result.stderr


# Flakes.


def test_flake_target():
    result = evalanche("fuzz", f"{FLAKE}#service")
    assert_baseline_passed(result)
    assert "kind modules, lib pinned" in result.stderr


def test_flake_relative_reference():
    result = evalanche("fuzz", "./flake#service")
    assert_baseline_passed(result)


def test_flake_baseline_failure():
    result = evalanche("fuzz", "./flake#broken")
    assert result.returncode == 2
    assert "baseline evaluation of `./flake#broken` failed" in result.stderr


def test_flake_unknown_target_name():
    result = evalanche("fuzz", "./flake#absent")
    assert result.returncode == 2
    assert "flake `./flake` has no target `absent`" in result.stderr


def test_flake_nixpkgs_input_supplies_lib():
    result = evalanche("list", "--json", "./flake-nixpkgs")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {
            "name": "service",
            "kind": "modules",
            "lib": {"source": "inputs.nixpkgs", "version": "fixture-input"},
        }
    ]


def test_dotted_fragment_is_a_usage_error():
    result = evalanche("fuzz", "./flake#evalancheTargets.service")
    assert result.returncode == 2
    assert "usage error" in result.stderr


def test_flake_without_flake_nix():
    result = evalanche("fuzz", "./modules#service")
    assert result.returncode == 2
    assert "flake `./modules` has no `flake.nix`" in result.stderr


# list.


def test_list_flake():
    result = evalanche("list", "./flake")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["broken", "service"]


def test_list_defaults_to_the_current_flake():
    result = evalanche("list", cwd=FLAKE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["broken", "service"]


def test_list_file():
    result = evalanche("list", "-f", str(TARGET_FILE))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["broken", "greeting", "service"]


def test_list_json():
    result = evalanche("list", "--json", "-f", str(TARGET_FILE))
    assert result.returncode == 0, result.stderr
    entries = json.loads(result.stdout)
    assert [entry["name"] for entry in entries] == ["broken", "greeting", "service"]
    for entry in entries:
        assert entry["kind"] == "modules"
        assert entry["lib"]["source"] == "pinned"
        assert isinstance(entry["lib"]["version"], str)


def test_list_bad_target_is_a_target_error():
    result = evalanche("list", "-f", str(BAD_TARGET_FILE))
    assert result.returncode == 2
    assert "target error" in result.stderr


@pytest.mark.parametrize("command", ["replay"])
def test_unimplemented_commands(command):
    result = evalanche(command)
    assert result.returncode == 2
    assert f"not implemented: `{command}`" in result.stderr


# Schema export.


@pytest.mark.parametrize("name", ["scalars", "constructors", "submodules"])
def test_schema_matches_expected(name):
    result = evalanche("schema", f"fixtures/{name}.nix", cwd=SCHEMA)
    assert result.returncode == 0, result.stderr
    expected = json.loads((SCHEMA / "expected" / f"{name}.json").read_text())
    assert json.loads(result.stdout) == expected


def test_schema_fingerprint_depth():
    result = evalanche("schema", "--fingerprint-depth", "4", "modules/port.nix")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["fingerprintDepth"] == 4


def test_schema_fingerprint_depth_must_be_positive():
    result = evalanche("schema", "--fingerprint-depth", "0", "modules/port.nix")
    assert result.returncode == 2
    assert "expected a positive integer" in result.stderr


def test_schema_file_target():
    result = evalanche("schema", "-f", str(TARGET_FILE), "service")
    assert result.returncode == 0, result.stderr
    schema = json.loads(result.stdout)
    assert [option["path"] for option in schema["options"]] == [["name"], ["port"]]


def test_schema_file_target_with_special_args():
    result = evalanche("schema", "-f", str(TARGET_FILE), "greeting")
    assert result.returncode == 0, result.stderr


def test_schema_flake_target():
    result = evalanche("schema", "./flake#service")
    assert result.returncode == 0, result.stderr
    schema = json.loads(result.stdout)
    assert [option["path"] for option in schema["options"]] == [["port"]]


def test_schema_does_not_evaluate_config():
    result = evalanche("schema", "modules/broken.nix")
    assert result.returncode == 0, result.stderr


def test_schema_export_failure_is_a_target_error():
    result = evalanche("schema", "modules/broken-type.nix")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "target error: schema export of `modules/broken-type.nix` failed" in (
        result.stderr
    )
    assert "cppnix: broken functor" in result.stderr
    assert "broken-type.nix:8:17" in result.stderr


def test_schema_bad_target_is_a_target_error():
    result = evalanche("schema", "-f", str(BAD_TARGET_FILE), "prefixed")
    assert result.returncode == 2
    assert "target `prefixed`: unknown attribute `prefix`" in result.stderr
