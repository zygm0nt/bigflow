"""Compiles, reads, validates `requirements.txt` files.
"""

import re
import typing
import logging
import tempfile
import textwrap
import hashlib
import subprocess

from pathlib import Path
from typing import Dict, List

import bigflow.commons as bf_commons


logger = logging.getLogger(__name__)


def pip_compile(
    requiremenets: Path,
    *,
    dry_run=False,
    verbose=False,
    upgrade=False,
    upgrade_package="",
    prereleases=False,
    rebuild=False,
    extra_args=(),
):
    """Wraps 'pip-tools' command. Include hash of source file into the generated one."""

    requirements_txt = requiremenets.with_suffix(".txt")
    requirements_in = requiremenets.with_suffix(".in")
    logger.info("Compile requirements file %s ...", requirements_in)

    with tempfile.NamedTemporaryFile('w+t', prefix=f"{requirements_in.stem}-", suffix=".txt", delete=False) as txt_file:
        txt_path = Path(txt_file.name)

        if requirements_txt.exists() and not rebuild:
            txt_path.write_bytes(requirements_txt.read_bytes())

        bf_commons.run_process([
            "pip-compile",
            "--no-header",
            *(["-o", txt_path] if not dry_run else []),
            *(["--dry-run"] if dry_run else []),
            *(["--rebuild"] if rebuild else []),
            *(["--upgrade"] if upgrade else []),
            *(["--pre"] if prereleases else []),
            *(["--upgrade-package", upgrade_package] if upgrade_package else []),
            *(["-v"] if verbose else ["-q"]),
            *extra_args,
            str(requirements_in),
        ], check=True)

        reqs_content = txt_path.read_text()

    if dry_run:
        return

    source_hash = compute_requirements_in_hash(requirements_in)
    with open(requirements_txt, 'w+t') as out:
        logger.info("Write pip requirements file: %s", requirements_txt)
        out.write(textwrap.dedent(f"""\
            # *** autogenerated: don't edit ***
            # $source-hash: {source_hash}
            # $source-file: {requirements_in}
            #
            # run 'bigflow build-requirements {requirements_in}' to update this file

        """))
        out.write(reqs_content)


def detect_piptools_source_files(requirements_dir: Path) -> typing.List[Path]:
    in_files = list(requirements_dir.glob("*.in"))

    manifest_file = requirements_dir / "MANIFEST.in"
    if manifest_file in in_files:
        in_files.remove(manifest_file)

    logger.debug("Found %d *.in files: %s", len(in_files), in_files)
    return in_files


def maybe_recompile_requirements_file(requirements_txt: Path) -> bool:
    # Some users keeps extra ".txt" files in the same directory.
    # Check if thoose files needs to be recompiled & then print a warning.
    for fin in detect_piptools_source_files(requirements_txt.parent):
        if fin.stem != requirements_txt.stem:
            check_requirements_needs_recompile(fin.with_suffix(".txt"))

    if check_requirements_needs_recompile(requirements_txt):
        pip_compile(requirements_txt)
        return True
    else:
        logger.debug("File %s is fresh", requirements_txt)
        return False


def _collect_all_input_files_content(requirements_in: Path):
    logger.debug("Scan all requiremenets.in-like files: %s", requirements_in)
    c = requirements_in.read_text()
    yield c
    for include in re.findall(r"\s*-r\s+(.*)", c):
        include: str
        fn = include.split("#", 1)[0].strip()
        yield from _collect_all_input_files_content(requirements_in.parent / fn)


def compute_requirements_in_hash(requirements_in: Path):
    logger.debug("Calculate hash of %s", requirements_in)
    algorithm = 'sha256'
    h = hashlib.new(algorithm)
    for c in _collect_all_input_files_content(requirements_in):
        h.update(c.encode())
    return algorithm + ":" + h.hexdigest()


def check_requirements_needs_recompile(requiremenets: Path) -> bool:
    """Checks if `requirements.{in,txt}` needs to be recompiled by `pip_compile()`"""

    requirements_txt = requiremenets.with_suffix(".txt")
    requirements_in = requiremenets.with_suffix(".in")
    logger.debug("Check if file %s should be recompiled", requirements_txt)

    if not requirements_in.exists():
        logger.debug("No file %s - pip-tools is not used", requirements_in)
        return False

    if not requirements_txt.exists():
        logger.debug("File %s does not exist - need to be compiled by 'pip-compile'", requirements_txt)
        return True

    requirements_txt_content = requirements_txt.read_text()
    hash1 = compute_requirements_in_hash(requirements_in)
    same_hash = hash1 in requirements_txt_content

    if same_hash:  # dirty but works ;)
        logger.debug("Don't need to compile %s file", requirements_txt)
        return False
    else:
        logger.warning("File %s needs to be recompiled with 'bigflow build-requirements' command", requirements_txt)
        return True


def read_requirements(requirements_path: Path, recompile_check=True) -> List[str]:
    """Reads and parses 'requirements.txt' file.

    Returns list of requirement specs, skipping comments and empty lines
    """

    if recompile_check and check_requirements_needs_recompile(requirements_path):
        raise ValueError("Requirements needs to be recompiled with 'pip-tools'")

    result: List[str] = []
    with open(requirements_path) as base_requirements:
        for line in base_requirements:
            line = line.split("#", 1)[0].strip()
            if line.startswith("-r "):
                subrequirements_file_name = line.replace("-r ", "")
                subrequirements_path = requirements_path.parent / subrequirements_file_name
                result.extend(read_requirements(subrequirements_path, recompile_check=False))
            elif line:
                result.append(line)

    return result


def generate_pinfile(
    requirements_path: Path,
    pins_file_in: Path,
    resolve_pins: typing.Callable[[], List[str]],
):
    requirements_txt = requirements_path.with_suffix(".txt")
    requirements_in = requirements_path.with_suffix(".in")

    logger.info("Clean pins file %s", pins_file_in)
    pins_file_in.write_text("# autocleaned ...")
    current_requirements_txt = requirements_txt.read_text()
    pip_compile(requirements_in, rebuild=True)

    pins = resolve_pins()
    logger.info("Found %d pins: %s", len(pins), ", ".join(pins))
    requirements_txt.write_text(current_requirements_txt)  # keep old .txt - preserve upgrades

    _include_pinsfile_into_requirements(pins_file_in, requirements_in)
    bad_pins, all_pins = _try_incrementally_add_pins(pins_file_in, requirements_in, pins)

    logger.info("\nWrite %s", pins_file_in)
    pins_file_in.write_text("\n".join([
        f"# *** autogenerated ***",
        *all_pins,
    ]))

    logger.info("Recompile requirements...")
    pip_compile(requirements_in, rebuild=True)

    if bad_pins:
        libs_list = "\n".join(f" - {x}" for x in sorted(bad_pins))
        logger.error("Failed to pin some libraries: \n%s", libs_list)
        logger.error("You may try to remove unused dependencies, upgrade beam or bigflow.")

    logger.info("Done")


def _try_incrementally_add_pins(pins_file_in, requirements_in, pins):
    bad_pins = []
    all_pins = []

    for i, pin in enumerate(pins):
        logger.info("\n(%d/%d) Trying to pin %r ...", i + 1, len(pins), pin)
        logger.info("Temporarily add pin %s", pin)
        pins_file_in.write_text("\n".join([
            "# *** autogenerated - partial! ***",
            *all_pins,
            pin,
            "# ...",
        ]))
        try:
            pip_compile(requirements_in, dry_run=True)
        except subprocess.CalledProcessError:
            logger.error("CONFLICT, revert %s", requirements_in)
            all_pins.append(f"## {pin}  # CONFLICT")
            bad_pins.append(pin)
        else:
            logger.info("OK, keep pin %s", pin)
            all_pins.append(pin)

    return bad_pins, all_pins


def _include_pinsfile_into_requirements(pins_file_in, requirements_in):
    req = requirements_in.read_text()
    m = re.search(rf"\s+-r\s+{re.escape(pins_file_in.name)}\s*#.*$", req)
    if m:
        logger.info("Pins file %s is already included into %s", pins_file_in, requirements_in)
    else:
        logger.info("Include pins file %s into %s", pins_file_in, requirements_in)
        relative_pins_path = pins_file_in.relative_to(requirements_in.parent)
        requirements_in.write_text(f"{req}\n-r {relative_pins_path} #  added by `bigflow`")
