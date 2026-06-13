import os
import sys
import shutil
import argparse
import re
import shlex
from importlib import metadata

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier
from packaging.version import InvalidVersion, Version

import setup_common

# Get the absolute path of the current file's directory (Kohua_SS project directory)
project_directory = (
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if "setup" in os.path.dirname(os.path.abspath(__file__))
    else os.path.dirname(os.path.abspath(__file__))
)

# Add the project directory to the beginning of the Python search path
sys.path.insert(0, project_directory)

from kohya_gui.custom_logging import setup_logging

# Set up logging
log = setup_logging()
log.debug(f"Project directory set to: {project_directory}")

SKIPPED_REQUIREMENT_OPTIONS_WITH_VALUES = {
    "--extra-index-url",
    "--index-url",
    "--find-links",
    "--trusted-host",
    "--no-binary",
    "--only-binary",
    "-f",
    "-i",
}

SKIPPED_REQUIREMENT_OPTIONS_WITHOUT_VALUES = {
    "--prefer-binary",
    "--pre",
}


def _strip_inline_comment(line):
    """Remove normal inline comments without breaking URL fragments."""
    return re.sub(r"\s+#.*$", "", line).strip()


def _resolve_requirement_path(parent_file, included_file):
    if os.path.isabs(included_file):
        return included_file
    return os.path.normpath(os.path.join(os.path.dirname(parent_file), included_file))


def _read_requirements(requirements_file, seen=None):
    requirements_file = os.path.abspath(requirements_file)
    seen = seen or set()
    if requirements_file in seen:
        return
    seen.add(requirements_file)

    if not os.path.exists(requirements_file):
        raise FileNotFoundError(f"Requirements file not found: {requirements_file}")

    with open(requirements_file, "r", encoding="utf8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = _strip_inline_comment(raw_line.strip())
            if not line or line.startswith("#"):
                continue

            if line.startswith("-r ") or line.startswith("--requirement "):
                included_file = line.split(maxsplit=1)[1]
                yield from _read_requirements(
                    _resolve_requirement_path(requirements_file, included_file),
                    seen=seen,
                )
                continue

            yield requirements_file, line_number, line


def _requirement_from_editable(line):
    _, editable_target = line.split(maxsplit=1)
    if "://" in editable_target:
        return None

    setup_py = os.path.join(project_directory, editable_target, "setup.py")
    setup_py = os.path.abspath(setup_py)
    if not os.path.exists(setup_py):
        return None

    with open(setup_py, "r", encoding="utf8") as file:
        setup_text = file.read()

    match = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", setup_text)
    if match is None:
        return None

    return Requirement(match.group(1))


def _split_requirement_args(line):
    parts = shlex.split(line, comments=False, posix=True)
    requirements = []
    index = 0

    while index < len(parts):
        part = parts[index]
        if part in SKIPPED_REQUIREMENT_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(part.startswith(f"{option}=") for option in SKIPPED_REQUIREMENT_OPTIONS_WITH_VALUES):
            index += 1
            continue
        if part in SKIPPED_REQUIREMENT_OPTIONS_WITHOUT_VALUES:
            index += 1
            continue
        if part.startswith("-"):
            index += 1
            continue
        requirements.append(part)
        index += 1

    return requirements


def _parse_requirement_line(line):
    if line.startswith("-e ") or line.startswith("--editable "):
        editable_requirement = _requirement_from_editable(line)
        return [editable_requirement] if editable_requirement is not None else []

    all_skipped_options = (
        SKIPPED_REQUIREMENT_OPTIONS_WITH_VALUES
        | SKIPPED_REQUIREMENT_OPTIONS_WITHOUT_VALUES
    )
    if any(
        line.startswith(f"{option} ") or line.startswith(f"{option}=") or line == option
        for option in all_skipped_options
    ):
        return []

    try:
        return [Requirement(line)]
    except Exception:
        parsed_requirements = []
        for requirement_arg in _split_requirement_args(line):
            try:
                parsed_requirements.append(Requirement(requirement_arg))
            except Exception:
                log.warning(f"Skipping requirement that cannot be validated without pip: {requirement_arg}")
        return parsed_requirements


def _requirement_applies(requirement):
    if requirement.marker is None:
        return True
    return requirement.marker.evaluate(default_environment())


def _requirement_satisfied(requirement):
    try:
        installed_version = metadata.version(requirement.name)
    except metadata.PackageNotFoundError:
        return False, "not installed"

    if not requirement.specifier:
        return True, installed_version

    try:
        is_satisfied = requirement.specifier.contains(
            Version(installed_version),
            prereleases=True,
        )
    except (InvalidSpecifier, InvalidVersion):
        is_satisfied = requirement.specifier.contains(
            installed_version,
            prereleases=True,
        )

    return is_satisfied, installed_version


def validate_requirements(requirements_file):
    log.info(f"Validating installed requirements from {requirements_file}...")

    failures = []
    checked = 0

    for file_path, line_number, line in _read_requirements(requirements_file):
        for requirement in _parse_requirement_line(line):
            if not _requirement_applies(requirement):
                continue

            checked += 1
            satisfied, installed_version = _requirement_satisfied(requirement)
            if satisfied:
                continue

            failures.append(
                f"{os.path.relpath(file_path, project_directory)}:{line_number}: "
                f"{requirement} ({installed_version})"
            )

    if failures:
        log.error("Requirement validation failed. Run setup to install or update dependencies.")
        for failure in failures:
            log.error(f"Missing or incompatible requirement: {failure}")
        sys.exit(1)

    log.info(f"Requirement validation passed ({checked} packages checked).")

def check_path_with_space():
    """Check if the current working directory contains a space."""
    cwd = os.getcwd()
    log.debug(f"Current working directory: {cwd}")
    if " " in cwd:
        # Log an error if the current working directory contains spaces
        log.error(
            "The path in which this python code is executed contains one or many spaces. This is not supported for running kohya_ss GUI."
        )
        log.error(
            "Please move the repo to a path without spaces, delete the venv folder, and run setup.sh again."
        )
        log.error(f"The current working directory is: {cwd}")
        raise RuntimeError("Invalid path: contains spaces.")

def detect_toolkit():
    """Detect the available toolkit (NVIDIA, AMD, or Intel) and log the information."""
    log.debug("Detecting available toolkit...")
    # Check for NVIDIA toolkit by looking for nvidia-smi executable
    if shutil.which("nvidia-smi") or os.path.exists(
        os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe"
        )
    ):
        log.debug("nVidia toolkit detected")
        return "nVidia"
    # Check for AMD toolkit by looking for rocminfo executable
    elif shutil.which("rocminfo") or os.path.exists("/opt/rocm/bin/rocminfo"):
        log.debug("AMD toolkit detected")
        return "AMD"
    # Check for Intel toolkit by looking for SYCL or OneAPI indicators
    elif (
        shutil.which("sycl-ls")
        or os.environ.get("ONEAPI_ROOT")
        or os.path.exists("/opt/intel/oneapi")
    ):
        log.debug("Intel toolkit detected")
        return "Intel"
    # Default to CPU if no toolkit is detected
    else:
        log.debug("No specific GPU toolkit detected, defaulting to CPU")
        return "CPU"

def check_torch():
    """Check if torch is available and log the relevant information."""
    # Detect the available toolkit (e.g., NVIDIA, AMD, Intel, or CPU)
    toolkit = detect_toolkit()
    log.info(f"{toolkit} toolkit detected")

    try:
        # Import PyTorch
        log.debug("Importing PyTorch...")
        import torch

        ipex = None
        # Attempt to import Intel Extension for PyTorch if Intel toolkit is detected
        if toolkit == "Intel":
            try:
                log.debug("Attempting to import Intel Extension for PyTorch (IPEX)...")
                import intel_extension_for_pytorch as ipex
                log.debug("Intel Extension for PyTorch (IPEX) imported successfully")
            except ImportError:
                log.warning("Intel Extension for PyTorch (IPEX) not found.")
        
        # Log the PyTorch version
        log.info(f"Torch {torch.__version__}")

        # Check if CUDA (NVIDIA GPU) is available
        if torch.cuda.is_available():
            log.debug("CUDA is available, logging CUDA info...")
            log_cuda_info(torch)
        # Check if XPU (Intel GPU) is available
        elif hasattr(torch, "xpu") and torch.xpu.is_available():
            log.debug("XPU is available, logging XPU info...")
            log_xpu_info(torch, ipex)
        # Log a warning if no GPU is available
        elif hasattr(torch, "mps") and torch.mps.is_available():
            log.info("MPS is available, logging MPS info...")
            log_mps_info(torch)
        else:
            log.warning("Torch reports GPU not available")

        # Return the major version of PyTorch
        return int(torch.__version__[0])
    except ImportError as e:
        # Log an error if PyTorch cannot be loaded
        log.error(f"Could not load torch: {e}")
        sys.exit(1)
    except Exception as e:
        # Log an unexpected error
        log.error(f"Unexpected error while checking torch: {e}")
        sys.exit(1)

def log_cuda_info(torch):
    """Log information about CUDA-enabled GPUs."""
    # Log the CUDA and cuDNN versions if available
    if torch.version.cuda:
        log.info(
            f'Torch backend: nVidia CUDA {torch.version.cuda} cuDNN {torch.backends.cudnn.version() if torch.backends.cudnn.is_available() else "N/A"}'
        )
    # Log the ROCm HIP version if using AMD GPU
    elif torch.version.hip:
        log.info(f"Torch backend: AMD ROCm HIP {torch.version.hip}")
    else:
        log.warning("Unknown Torch backend")

    # Log information about each detected CUDA-enabled GPU
    for device in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(device)
        log.info(
            f"Torch detected GPU: {props.name} VRAM {round(props.total_memory / 1024 / 1024)}MB Arch {props.major}.{props.minor} Cores {props.multi_processor_count}"
        )

def log_mps_info(torch):
    """Log information about Apple Silicone (MPS)"""
    max_recommended_mem = round(torch.mps.recommended_max_memory() / 1024**2)
    log.info(
        f"Torch detected Apple MPS: {max_recommended_mem}MB Unified Memory Available"
    )
    log.warning('MPS support is still experimental, proceed with caution.')


def log_xpu_info(torch, ipex):
    """Log information about Intel XPU-enabled GPUs."""
    # Log the Intel Extension for PyTorch (IPEX) version if available
    if ipex:
        log.info(f"Torch backend: Intel IPEX {ipex.__version__}")
    # Log information about each detected XPU-enabled GPU
    for device in range(torch.xpu.device_count()):
        props = torch.xpu.get_device_properties(device)
        log.info(
            f"Torch detected GPU: {props.name} VRAM {round(props.total_memory / 1024 / 1024)}MB Compute Units {props.max_compute_units}"
        )

def main():
    # Check the repository version to ensure compatibility
    log.debug("Checking repository version...")
    setup_common.check_repo_version()
    # Check if the current path contains spaces, which are not supported
    log.debug("Checking if the current path contains spaces...")
    check_path_with_space()

    # Parse command line arguments
    log.debug("Parsing command line arguments...")
    parser = argparse.ArgumentParser(
        description="Validate that requirements are satisfied."
    )
    parser.add_argument(
        "-r", "--requirements", type=str, help="Path to the requirements file."
    )
    parser.add_argument("--debug", action="store_true", help="Debug on")
    args = parser.parse_args()

    # Check if PyTorch is installed and log relevant information
    log.debug("Checking if PyTorch is installed...")
    check_torch()

    # Check if the Python version is compatible
    log.debug("Checking Python version...")
    if not setup_common.check_python_version():
        sys.exit(1)

    # Validate required packages from the specified requirements file
    requirements_file = args.requirements or "requirements_pytorch_windows.txt"
    log.debug(f"Validating requirements from: {requirements_file}")
    validate_requirements(requirements_file)

if __name__ == "__main__":
    log.debug("Starting main function...")
    main()
    log.debug("Main function finished.")
