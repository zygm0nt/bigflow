"""Compiles, reads, validates `requirements.txt` files.
"""

import re
import subprocess
import typing
import logging
import tempfile
import textwrap
import hashlib

from pathlib import Path
from typing import Dict, List

import bigflow.commons as bf_commons


logger = logging.getLogger(__name__)


def pip_compile(
    req: Path,
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

    req_txt = req.with_suffix(".txt")
    req_in = req.with_suffix(".in")
    logger.info("Compile requirements file %s ...", req_in)

    with tempfile.NamedTemporaryFile('w+t', prefix=f"{req_in.stem}-", suffix=".txt", delete=False) as txt_file:
        txt_path = Path(txt_file.name)

        if req_txt.exists() and not rebuild:
            txt_path.write_bytes(req_txt.read_bytes())

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
            str(req_in),
        ], check=True)

        reqs_content = txt_path.read_text()

    if dry_run:
        return

    source_hash = reqin_file_hash(req_in)
    with open(req_txt, 'w+t') as out:
        logger.info("Write pip requirements file: %s", req_txt)
        out.write(textwrap.dedent(f"""\
            # *** autogenerated: don't edit ***
            # $source-hash: {source_hash}
            # $source-file: {req_in}
            #
            # run 'bigflow build-requirements {req_in}' to update this file

        """))
        out.write(reqs_content)


def detect_piptools_source_files(reqs_dir: Path) -> typing.List[Path]:
    in_files = list(reqs_dir.glob("*.in"))

    manifest_file = reqs_dir / "MANIFEST.in"
    if manifest_file in in_files:
        in_files.remove(manifest_file)

    logger.debug("Found %d *.in files: %s", len(in_files), in_files)
    return in_files


def maybe_recompile_requirements_file(req_txt: Path) -> bool:
    # Some users keeps extra ".txt" files in the same directory.
    # Check if thoose files needs to be recompiled & then print a warning.
    for fin in detect_piptools_source_files(req_txt.parent):
        if fin.stem != req_txt.stem:
            check_requirements_needs_recompile(fin.with_suffix(".txt"))

    if check_requirements_needs_recompile(req_txt):
        pip_compile(req_txt)
        return True
    else:
        logger.debug("File %s is fresh", req_txt)
        return False


def _collect_all_input_files_content(req_in: Path):
    logger.debug("Scan all req.in-like files: %s", req_in)
    c = req_in.read_text()
    yield c
    for include in re.findall(r"\s*-r\s+(.*)", c):
        include: str
        fn = include.split("#", 1)[0].strip()
        yield from _collect_all_input_files_content(req_in.parent / fn)


def reqin_file_hash(req_in: Path):
    logger.debug("Calculate hash of %s", req_in)
    algorithm = 'sha256'
    h = hashlib.new(algorithm)
    for c in _collect_all_input_files_content(req_in):
        h.update(c.encode())
    return algorithm + ":" + h.hexdigest()


def check_requirements_needs_recompile(req: Path) -> bool:
    """Checks if `requirements.{in,txt}` needs to be recompiled by `pip_compile()`"""

    req_txt = req.with_suffix(".txt")
    req_in = req.with_suffix(".in")
    logger.debug("Check if file %s should be recompiled", req_txt)

    if not req_in.exists():
        logger.debug("No file %s - pip-tools is not used", req_in)
        return False

    if not req_txt.exists():
        logger.debug("File %s does not exist - need to be compiled by 'pip-compile'", req_txt)
        return True

    req_txt_content = req_txt.read_text()
    hash1 = reqin_file_hash(req_in)
    same_hash = hash1 in req_txt_content

    if same_hash:  # dirty but works ;)
        logger.debug("Don't need to compile %s file", req_txt)
        return False
    else:
        logger.warning("File %s needs to be recompiled with 'bigflow build-requirements' command", req_txt)
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
    req_path: Path,
    pins_path: Path,
    get_pins: typing.Callable[[], List[str]],
):
    req_txt = req_path.with_suffix(".txt")
    req_in = req_path.with_suffix(".in")
    pins_in = pins_path.with_suffix(".in")

    logger.info("Clean pins file %s", pins_in)
    pins_in.write_text("# autocleaned ...")
    current_reqs_txt = req_txt.read_text()
    pip_compile(req_in, rebuild=True)

    pins = get_pins()
    logger.info("Found %d pins: %s", len(pins), ", ".join(pins))
    req_txt.write_text(current_reqs_txt)  # keep old .txt - preserve upgrades

    req = req_in.read_text()
    m = re.search(rf"\s+-r\s+{re.escape(pins_path.name)}\s*#.*$", req)
    if m:
        logger.info("Pins file %s is already included into %s", pins_path, req_in)
    else:
        logger.info("Include pins file %s into %s", pins_path, req_in)
        relative_pins_path = pins_path.relative_to(req_in.parent)
        req_in.write_text(f"{req}\n-r {relative_pins_path} #  added by `bigflow`")

    bad_pins = []
    good_pins = []
    all_pins = []

    for i, pin in enumerate(pins):
        logger.info("\n(%d/%d) Trying to pin %r ...", i + 1, len(pins), pin)

        logger.info("Temporarily add pin %s", pin)
        pins_path.write_text("\n".join([
            "# *** autogenerated - partial! ***",
            *all_pins,
            pin,
            "# ...",
        ]))

        try:
            pip_compile(req_in, dry_run=True)
        except subprocess.CalledProcessError:
            logger.error("CONFLICT, revert %s", req_in)
            all_pins.append(f"## {pin}  # CONFLICT")
            bad_pins.append(pin)
        else:
            logger.info("OK, keep pin %s", pin)
            good_pins.append(pin)
            all_pins.append(pin)

    logger.info("\nWrite %s", pins_path)
    pins_path.write_text("\n".join([
        f"# *** autogenerated ***",
        *all_pins,
    ]))

    logger.info("Recompile requirements...")
    pip_compile(req_in, rebuild=True)

    if bad_pins:
        libs_list = "\n".join(f" - {x}" for x in sorted(bad_pins))
        logger.error("Failed to pin some libraries: \n%s", libs_list)
        logger.error("You may try to remove unused dependencies, upgrade beam or bigflow.")

    logger.info("Done")
