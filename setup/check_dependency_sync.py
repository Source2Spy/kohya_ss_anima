import argparse
import ast
import re
import shlex
import sys
from pathlib import Path

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency 'packaging'. Run setup first or install packaging."
    ) from exc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
COMMON_REQUIREMENTS = PROJECT_ROOT / "requirements.txt"

# uv currently models the default CUDA paths only. ROCm, IPEX, XPU, RunPod and
# macOS stacks remain pip/setup requirements until they are split into uv extras.
UV_PLATFORM_REQUIREMENTS = {
    "win32": PROJECT_ROOT / "requirements_pytorch_windows.txt",
    "linux": PROJECT_ROOT / "requirements_linux.txt",
}

PIP_OPTIONS_WITH_VALUES = {
    "--extra-index-url",
    "--index-url",
    "--find-links",
    "--trusted-host",
    "--no-binary",
    "--only-binary",
    "-f",
    "-i",
}

PIP_OPTIONS_WITHOUT_VALUES = {
    "--prefer-binary",
    "--pre",
}


def strip_inline_comment(line):
    return re.sub(r"\s+#.*$", "", line).strip()


def resolve_requirement_path(parent_file, included_file):
    path = Path(included_file)
    if path.is_absolute():
        return path
    return (parent_file.parent / path).resolve()


def read_requirement_lines(requirements_file, seen=None):
    requirements_file = requirements_file.resolve()
    seen = seen or set()
    if requirements_file in seen:
        return
    seen.add(requirements_file)

    with requirements_file.open("r", encoding="utf8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = strip_inline_comment(raw_line.strip())
            if not line or line.startswith("#"):
                continue
            if line.startswith("-r ") or line.startswith("--requirement "):
                included_file = line.split(maxsplit=1)[1]
                yield from read_requirement_lines(
                    resolve_requirement_path(requirements_file, included_file),
                    seen=seen,
                )
                continue
            yield requirements_file, line_number, line


def requirement_from_editable(line):
    _, editable_target = line.split(maxsplit=1)
    if "://" in editable_target:
        return None

    setup_py = (PROJECT_ROOT / editable_target / "setup.py").resolve()
    if not setup_py.exists():
        return None

    setup_text = setup_py.read_text(encoding="utf8")
    match = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", setup_text)
    if match is None:
        return None

    return Requirement(match.group(1))


def split_requirement_args(line):
    parts = shlex.split(line, comments=False, posix=True)
    requirements = []
    index = 0

    while index < len(parts):
        part = parts[index]
        if part in PIP_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(part.startswith(f"{option}=") for option in PIP_OPTIONS_WITH_VALUES):
            index += 1
            continue
        if part in PIP_OPTIONS_WITHOUT_VALUES:
            index += 1
            continue
        if part.startswith("-"):
            index += 1
            continue
        requirements.append(part)
        index += 1

    return requirements


def parse_requirement_line(line):
    if line.startswith("-e ") or line.startswith("--editable "):
        editable_requirement = requirement_from_editable(line)
        return [editable_requirement] if editable_requirement is not None else []

    skipped_options = PIP_OPTIONS_WITH_VALUES | PIP_OPTIONS_WITHOUT_VALUES
    if any(
        line.startswith(f"{option} ") or line.startswith(f"{option}=") or line == option
        for option in skipped_options
    ):
        return []

    try:
        return [Requirement(line)]
    except Exception:
        parsed_requirements = []
        for requirement_arg in split_requirement_args(line):
            try:
                parsed_requirements.append(Requirement(requirement_arg))
            except Exception:
                print(
                    f"Skipping requirement that cannot be represented in pyproject: "
                    f"{requirement_arg}",
                    file=sys.stderr,
                )
        return parsed_requirements


def load_requirements(requirements_file):
    requirements = []
    for source_file, _line_number, line in read_requirement_lines(requirements_file):
        for requirement in parse_requirement_line(line):
            requirements.append((source_file, requirement))
    return requirements


def format_requirement_base(requirement):
    extras = ""
    if requirement.extras:
        extras = "[" + ",".join(sorted(requirement.extras)) + "]"
    return f"{requirement.name}{extras}{requirement.specifier}"


def with_platform_marker(requirement, platform):
    base = format_requirement_base(requirement)
    marker = f"sys_platform == '{platform}'"
    if requirement.marker is not None:
        marker = f"({requirement.marker}) and {marker}"
    return Requirement(f"{base}; {marker}")


def requirement_key(requirement):
    return (
        canonicalize_name(requirement.name),
        tuple(sorted(requirement.extras)),
        str(requirement.specifier),
        str(requirement.marker) if requirement.marker is not None else "",
    )


def requirement_label(requirement):
    marker = f"; {requirement.marker}" if requirement.marker is not None else ""
    return f"{format_requirement_base(requirement)}{marker}"


def dependency_string(requirement):
    marker = ""
    if requirement.marker is not None:
        marker = f"; {str(requirement.marker).replace(chr(34), chr(39))}"
    return f"{format_requirement_base(requirement)}{marker}"


def load_pyproject_dependencies():
    lines = PYPROJECT.read_text(encoding="utf8").splitlines()
    in_project = False
    collecting = False
    dependency_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("[") and stripped.endswith("]"):
            break

        if not in_project:
            continue

        if not collecting and stripped.startswith("dependencies"):
            collecting = True
            dependency_lines.append(line.split("=", 1)[1].strip())
            continue

        if collecting:
            dependency_lines.append(stripped)
            if stripped == "]":
                break

    if not dependency_lines:
        raise SystemExit("Could not find [project].dependencies in pyproject.toml.")

    return ast.literal_eval("\n".join(dependency_lines))


def expected_pyproject_requirements():
    expected = [requirement for _source, requirement in load_requirements(COMMON_REQUIREMENTS)]

    for platform, requirements_file in UV_PLATFORM_REQUIREMENTS.items():
        for source_file, requirement in load_requirements(requirements_file):
            if source_file.resolve() == COMMON_REQUIREMENTS.resolve():
                continue
            expected.append(with_platform_marker(requirement, platform))

    return expected


def actual_pyproject_requirements():
    return [Requirement(dependency) for dependency in load_pyproject_dependencies()]


def render_dependencies(dependencies):
    rendered = ["dependencies = ["]
    for dependency in dependencies:
        rendered.append(f'    "{dependency_string(dependency)}",')
    rendered.append("]")
    return rendered


def write_pyproject_dependencies(dependencies):
    lines = PYPROJECT.read_text(encoding="utf8").splitlines()
    in_project = False
    start = None
    end = None

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("[") and stripped.endswith("]"):
            break
        if in_project and stripped.startswith("dependencies"):
            start = index
            break

    if start is None:
        raise SystemExit("Could not find [project].dependencies in pyproject.toml.")

    for index in range(start, len(lines)):
        if lines[index].strip() == "]":
            end = index
            break

    if end is None:
        raise SystemExit("Could not find end of [project].dependencies in pyproject.toml.")

    replacement = render_dependencies(dependencies)
    PYPROJECT.write_text(
        "\n".join(lines[:start] + replacement + lines[end + 1 :]) + "\n",
        encoding="utf8",
    )


def sync_pyproject_dependencies():
    write_pyproject_dependencies(expected_pyproject_requirements())


def main():
    parser = argparse.ArgumentParser(
        description="Check or regenerate pyproject.toml dependencies from requirements files."
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Rewrite [project].dependencies in pyproject.toml from requirements files.",
    )
    args = parser.parse_args()

    expected_requirements = expected_pyproject_requirements()
    if args.fix:
        sync_pyproject_dependencies()
        print("Updated pyproject.toml dependencies from requirements files.")

    expected = {requirement_key(req): req for req in expected_requirements}
    actual = {requirement_key(req): req for req in actual_pyproject_requirements()}

    missing = [expected[key] for key in sorted(expected.keys() - actual.keys())]
    extra = [actual[key] for key in sorted(actual.keys() - expected.keys())]

    if not missing and not extra:
        print("pyproject.toml dependencies are in sync with requirements files.")
        return

    if missing:
        print("Missing from pyproject.toml:")
        for requirement in missing:
            print(f"  {requirement_label(requirement)}")

    if extra:
        print("Extra in pyproject.toml:")
        for requirement in extra:
            print(f"  {requirement_label(requirement)}")

    print("Run `python setup/check_dependency_sync.py --fix` to regenerate pyproject.toml.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
