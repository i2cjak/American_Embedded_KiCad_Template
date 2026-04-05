#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


class BuildError(RuntimeError):
    pass


class StepFailed(BuildError):
    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass
class StepOutcome:
    returncode: int
    output: str = ""


class BuildRunner:
    def __init__(self, project_file: Path | None, theme_override: str | None) -> None:
        self.cwd = Path.cwd()
        self.project_file = self._resolve_project_file(project_file)
        self.project_name = self.project_file.name.removesuffix(".kicad_pro")
        self.schematic_file = self.cwd / f"{self.project_name}.kicad_sch"
        self.pcb_file = self.cwd / f"{self.project_name}.kicad_pcb"

        self.build_dir = self.cwd / "build"
        self.gerber_dir = self.build_dir / "gerbs"
        self.gerber_zip = self.build_dir / "gerbers.zip"
        self.raw_positions_file = self.build_dir / "positions_raw.csv"
        self.positions_file = self.build_dir / "positions.csv"
        self.invalid_build_file = self.build_dir / "INVALID_BUILD"

        self.schematic_theme = (
            theme_override if theme_override is not None else os.environ.get("SCHEMATIC_THEME", "American Embedded Light")
        )

        self.invalid_reasons: list[str] = []
        self.invalid_notified = False

        self._require_command("kicad-cli")
        self._require_file(self.project_file)
        self._require_file(self.schematic_file)
        self._require_file(self.pcb_file)

        self.board_layers = self._extract_board_layers()
        if not self.board_layers:
            raise BuildError(f"Failed to extract layer list from '{self.pcb_file.name}'.")

    def run(self) -> int:
        if self.project_name == "Template":
            print("Warning: The project name is still 'Template'.")
            print("It is recommended to rename project files before proceeding.")
            print()

        self._prepare_build_dir()

        print(f"Using KiCad CLI {self._kicad_version()}")
        print(f"Project: {self.project_file.name}")
        print()

        gerber_layers = self._collect_layers(
            [
                "B.Cu",
                "B.Mask",
                "B.Paste",
                "B.SilkS",
                "B.CrtYd",
                "B.Fab",
                *self._inner_copper_layers(),
                "F.Cu",
                "F.Mask",
                "F.Paste",
                "F.SilkS",
                "F.CrtYd",
                "F.Fab",
                "Dwgs.User",
                "Cmts.User",
                "Edge.Cuts",
            ]
        )
        pcb_pdf_layers = self._collect_layers(
            [
                "B.Cu",
                "B.Mask",
                "B.Paste",
                "B.SilkS",
                "B.CrtYd",
                "B.Fab",
                *self._inner_copper_layers(),
                "F.Cu",
                "F.Mask",
                "F.Paste",
                "F.SilkS",
                "F.CrtYd",
                "F.Fab",
                "Dwgs.User",
                "Cmts.User",
                "User.4",
                "User.3",
                "User.2",
                "User.1",
                "Edge.Cuts",
            ]
        )

        self._run_step(
            "ERC",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "sch",
                    "erc",
                    "--output",
                    str(self.build_dir / f"{self.project_name}-erc.rpt"),
                    "--format",
                    "report",
                    "--units",
                    "mm",
                    "--severity-warning",
                    "--severity-error",
                    "--exit-code-violations",
                    str(self.schematic_file),
                ]
            ),
            invalidates_build=True,
        )

        self._run_step(
            "DRC",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "pcb",
                    "drc",
                    "--output",
                    str(self.build_dir / f"{self.project_name}-drc.rpt"),
                    "--format",
                    "report",
                    "--units",
                    "mm",
                    "--severity-warning",
                    "--severity-error",
                    "--refill-zones",
                    "--schematic-parity",
                    "--exit-code-violations",
                    str(self.pcb_file),
                ]
            ),
            invalidates_build=True,
        )

        schematic_pdf_cmd = [
            "kicad-cli",
            "sch",
            "export",
            "pdf",
            "--output",
            str(self.build_dir / "schematic.pdf"),
        ]
        if self.schematic_theme:
            schematic_pdf_cmd.extend(["--theme", self.schematic_theme])
        schematic_pdf_cmd.append(str(self.schematic_file))

        self._run_step("Schematic PDF", lambda: self._run_command(schematic_pdf_cmd))

        self._run_step(
            "JLCPCB BOM",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "sch",
                    "export",
                    "bom",
                    "--preset",
                    "JLCPCB",
                    "--format-preset",
                    "CSV",
                    "--output",
                    str(self.build_dir / "bom_JLCPCB.csv"),
                    str(self.schematic_file),
                ]
            ),
        )

        self._run_step(
            "NextPCB BOM",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "sch",
                    "export",
                    "bom",
                    "--preset",
                    "NextPCB",
                    "--format-preset",
                    "CSV",
                    "--output",
                    str(self.build_dir / "bom_NextPCB.csv"),
                    str(self.schematic_file),
                ]
            ),
        )

        self._run_step(
            "Gerbers",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "pcb",
                    "export",
                    "gerbers",
                    "--output",
                    str(self.gerber_dir),
                    "--layers",
                    gerber_layers,
                    "--crossout-DNP-footprints-on-fab-layers",
                    "--sketch-DNP-footprints-on-fab-layers",
                    "--subtract-soldermask",
                    "--precision",
                    "5",
                    str(self.pcb_file),
                ]
            ),
        )

        self._run_step(
            "Drill files",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "pcb",
                    "export",
                    "drill",
                    "--output",
                    str(self.gerber_dir),
                    "--format",
                    "excellon",
                    "--drill-origin",
                    "absolute",
                    "--excellon-units",
                    "in",
                    "--excellon-zeros-format",
                    "decimal",
                    "--gerber-precision",
                    "5",
                    str(self.pcb_file),
                ]
            ),
        )

        self._run_step("Gerber archive", self._create_gerber_archive)

        self._run_step(
            "PCB PDF",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "pcb",
                    "export",
                    "pdf",
                    "--output",
                    str(self.build_dir / "pcb_layout"),
                    "--layers",
                    pcb_pdf_layers,
                    "--black-and-white",
                    "--crossout-DNP-footprints-on-fab-layers",
                    "--sketch-DNP-footprints-on-fab-layers",
                    "--include-border-title",
                    "--drill-shape-opt",
                    "2",
                    "--mode-multipage",
                    str(self.pcb_file),
                ]
            ),
        )

        self._run_step(
            "Raw positions",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "pcb",
                    "export",
                    "pos",
                    "--output",
                    str(self.raw_positions_file),
                    "--format",
                    "csv",
                    "--units",
                    "mm",
                    "--side",
                    "both",
                    "--exclude-dnp",
                    "--use-drill-file-origin",
                    str(self.pcb_file),
                ]
            ),
        )

        self._run_step("Placement CSV", self._convert_positions)

        self._run_step(
            "STEP model",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "pcb",
                    "export",
                    "step",
                    "--output",
                    str(self.build_dir / "board.step"),
                    "--force",
                    "--subst-models",
                    "--no-dnp",
                    str(self.pcb_file),
                ]
            ),
        )

        self._run_step(
            "Top render",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "pcb",
                    "render",
                    "--output",
                    str(self.build_dir / "top.png"),
                    "--width",
                    "1280",
                    "--height",
                    "720",
                    "--side",
                    "top",
                    "--background",
                    "transparent",
                    "--quality",
                    "basic",
                    "--preset",
                    "follow_pcb_editor",
                    "--light-top",
                    "0",
                    "--light-bottom",
                    "0",
                    "--light-side",
                    "0.5",
                    "--light-camera",
                    "0",
                    "--light-side-elevation",
                    "60",
                    str(self.pcb_file),
                ]
            ),
        )

        self._run_step(
            "Bottom render",
            lambda: self._run_command(
                [
                    "kicad-cli",
                    "pcb",
                    "render",
                    "--output",
                    str(self.build_dir / "bottom.png"),
                    "--width",
                    "1280",
                    "--height",
                    "720",
                    "--side",
                    "bottom",
                    "--background",
                    "transparent",
                    "--quality",
                    "basic",
                    "--preset",
                    "follow_pcb_editor",
                    "--light-top",
                    "0",
                    "--light-bottom",
                    "0",
                    "--light-side",
                    "0.5",
                    "--light-camera",
                    "0",
                    "--light-side-elevation",
                    "60",
                    str(self.pcb_file),
                ]
            ),
        )

        print("Build completed.")
        return 0

    def _run_step(self, label: str, action, *, invalidates_build: bool = False) -> StepOutcome:
        print(f"[{label}]")

        try:
            outcome = action()
        except BuildError as exc:
            outcome = StepOutcome(1, f"Error: {exc}\n")

        if outcome.output:
            sys.stdout.write(outcome.output)
            if not outcome.output.endswith("\n"):
                print()

        print()

        if outcome.returncode != 0:
            if invalidates_build:
                self._mark_invalid(f"{label} failed (exit {outcome.returncode})")
                self._handle_invalid_build_status()

            sys.stdout.flush()
            self._print_error(f"{label} failed (exit {outcome.returncode}).")
            raise StepFailed(f"{label} failed", outcome.returncode)

        return outcome

    def _run_command(self, command: list[str]) -> StepOutcome:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        return StepOutcome(completed.returncode, completed.stdout)

    def _convert_positions(self) -> StepOutcome:
        if not self.raw_positions_file.is_file():
            raise BuildError(f"Raw position file was not created: '{self.raw_positions_file.name}'")

        with self.raw_positions_file.open(newline="", encoding="utf-8") as src, self.positions_file.open(
            "w", newline="", encoding="utf-8"
        ) as dst:
            reader = csv.DictReader(src)
            required = {"Ref", "PosX", "PosY", "Rot", "Side"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise BuildError(
                    f"Unexpected position CSV header in '{self.raw_positions_file.name}': {reader.fieldnames!r}"
                )

            writer = csv.writer(dst)
            writer.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])

            for row in reader:
                side = (row["Side"] or "").strip().lower()
                layer = "T" if side in {"top", "front"} else "B"
                writer.writerow([row["Ref"], row["PosX"], row["PosY"], layer, row["Rot"]])

        return StepOutcome(0, f"Converted positions: {self.positions_file.relative_to(self.cwd)}\n")

    def _create_gerber_archive(self) -> StepOutcome:
        if not self.gerber_dir.is_dir():
            raise BuildError(f"Gerber directory was not created: '{self.gerber_dir.name}'")

        with zipfile.ZipFile(self.gerber_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.gerber_dir.iterdir()):
                if path.is_file():
                    archive.write(path, arcname=path.name)

        return StepOutcome(0, f"Created archive '{self.gerber_zip.relative_to(self.cwd)}'.\n")

    def _prepare_build_dir(self) -> None:
        self.build_dir.mkdir(exist_ok=True)
        shutil.rmtree(self.gerber_dir, ignore_errors=True)
        self.gerber_dir.mkdir()

        for path in [
            self.gerber_zip,
            self.raw_positions_file,
            self.positions_file,
            self.invalid_build_file,
            self.build_dir / "positions_raw-all-pos",
            self.build_dir / "positions_raw-all-pos.csv",
            self.build_dir / "schematic.pdf",
            self.build_dir / "bom_JLCPCB.csv",
            self.build_dir / "bom_NextPCB.csv",
            self.build_dir / "board.step",
            self.build_dir / "pcb_layout",
            self.build_dir / "top.png",
            self.build_dir / "bottom.png",
        ]:
            path.unlink(missing_ok=True)

        for pattern in ("*-erc.rpt", "*-drc.rpt"):
            for path in self.build_dir.glob(pattern):
                path.unlink(missing_ok=True)

    def _resolve_project_file(self, project_file: Path | None) -> Path:
        if project_file is not None:
            resolved = (self.cwd / project_file).resolve() if not project_file.is_absolute() else project_file.resolve()
            if resolved.parent != self.cwd:
                raise BuildError("Project file must be in the current working directory.")
            return resolved

        project_files = sorted(self.cwd.glob("*.kicad_pro"))
        if not project_files:
            raise BuildError("No .kicad_pro file found in the current directory.")
        if len(project_files) > 1:
            names = "\n".join(path.name for path in project_files)
            raise BuildError(f"Multiple .kicad_pro files found in this directory:\n{names}")
        return project_files[0]

    def _extract_board_layers(self) -> list[str]:
        layers: list[str] = []
        in_layers = False

        with self.pcb_file.open(encoding="utf-8") as pcb:
            for line in pcb:
                stripped = line.strip()
                if stripped == "(layers":
                    in_layers = True
                    continue
                if in_layers and stripped == ")":
                    break
                if in_layers:
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        layers.append(match.group(1))

        return layers

    def _inner_copper_layers(self) -> list[str]:
        inner_layers = [layer for layer in self.board_layers if re.fullmatch(r"In\d+\.Cu", layer)]
        return sorted(inner_layers, key=lambda layer: int(layer[2:].split(".")[0]), reverse=True)

    def _collect_layers(self, candidates: list[str]) -> str:
        board_layer_set = set(self.board_layers)
        return ",".join(layer for layer in candidates if layer in board_layer_set)

    def _handle_invalid_build_status(self) -> None:
        if not self.invalid_reasons:
            self.invalid_build_file.unlink(missing_ok=True)
            return

        self._update_invalid_build_file()
        self._notify_invalid_build()

    def _mark_invalid(self, reason: str) -> None:
        if reason not in self.invalid_reasons:
            self.invalid_reasons.append(reason)
            self._update_invalid_build_file()

    def _update_invalid_build_file(self) -> None:
        if not self.invalid_reasons:
            self.invalid_build_file.unlink(missing_ok=True)
            return

        lines = [
            f"Project: {self.project_file.name}",
            "Status: INVALID_BUILD",
            "Reason: ERC and/or DRC failed.",
            "",
            "Failures:",
            *[f"- {reason}" for reason in self.invalid_reasons],
            "",
            f"Reports: {self.project_name}-erc.rpt, {self.project_name}-drc.rpt",
        ]
        self.invalid_build_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _notify_invalid_build(self) -> None:
        if self.invalid_notified:
            return

        title = "KiCad Build Invalid"
        body = f"{self.project_name}: ERC/DRC failed. See build/INVALID_BUILD."

        notification_sent = False
        if shutil.which("notify-send") is not None:
            completed = subprocess.run(["notify-send", title, body], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            notification_sent = completed.returncode == 0

        if not notification_sent:
            print(f"Notification: {title}: {body}", file=sys.stderr)

        self.invalid_notified = True

    def _kicad_version(self) -> str:
        completed = subprocess.run(["kicad-cli", "version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        version = completed.stdout.strip()
        return version or "unknown"

    @staticmethod
    def _require_command(name: str) -> None:
        if shutil.which(name) is None:
            raise BuildError(f"Required command not found: '{name}'")

    @staticmethod
    def _require_file(path: Path) -> None:
        if not path.is_file():
            raise BuildError(f"Required file not found: '{path.name}'")

    @staticmethod
    def _print_error(message: str) -> None:
        print(f"{ANSI_RED}ERROR:{ANSI_RESET} {message}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build KiCad outputs directly with kicad-cli.")
    parser.add_argument(
        "--project-file",
        type=Path,
        default=None,
        help="Project file in the current directory. Defaults to the only *.kicad_pro file found.",
    )
    parser.add_argument(
        "--theme",
        default=None,
        help="Schematic PDF theme override. Defaults to SCHEMATIC_THEME or 'American Embedded Light'.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        runner = BuildRunner(args.project_file, theme_override=args.theme)
        return runner.run()
    except StepFailed as exc:
        return exc.returncode
    except BuildError as exc:
        BuildRunner._print_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
