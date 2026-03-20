from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from .model import SimulatorStatus
from .service import VirtualLinienControlService


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    unit: str
    step: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    precision: int = 4
    setter: Callable[[VirtualLinienControlService, Any], None] | None = None
    choices: tuple[str, ...] | None = None


class ValueEditScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    ValueEditScreen {
        align: center middle;
        background: $background 60%;
    }
    #value-dialog {
        width: 72;
        max-width: 90%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #value-actions {
        align-horizontal: right;
    }
    """

    def __init__(self, label: str, initial_value: str) -> None:
        super().__init__()
        self._label = label
        self._initial_value = initial_value

    def compose(self) -> ComposeResult:
        with Vertical(id="value-dialog"):
            yield Static(f"Set {self._label}")
            yield Input(value=self._initial_value, id="value-input")
            with Horizontal(id="value-actions"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Apply", id="apply", variant="primary")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#apply")
    def _apply(self) -> None:
        value = self.query_one("#value-input", Input).value.strip()
        self.dismiss(value)

    @on(Input.Submitted, "#value-input")
    def _apply_submit(self) -> None:
        value = self.query_one("#value-input", Input).value.strip()
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LinienSimTui(App[None]):
    TITLE = "Linien Simulator TUI"
    SUB_TITLE = "Arrow keys and Enter to edit values"

    BINDINGS = [
        Binding("left", "decrement", "Decrease", priority=True),
        Binding("right", "increment", "Increase", priority=True),
        Binding("enter", "edit_selected", "Edit", priority=True),
        Binding("l", "lock_mode", "Lock"),
        Binding("s", "sweep_mode", "Sweep"),
        Binding("q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    #main-layout {
        height: 1fr;
    }
    #fields-pane {
        width: 62%;
        padding: 0 1 0 0;
    }
    #status-pane {
        width: 38%;
    }
    #status-box {
        height: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    #message-box {
        height: auto;
        min-height: 1;
        padding: 0 1;
        color: $warning;
    }
    """

    def __init__(self, service: VirtualLinienControlService) -> None:
        super().__init__()
        self._service = service
        self._command_values: dict[str, float] = {
            "kick_delta_v": 0.02,
            "step_disturbance_v": 0.02,
        }
        self._fields = self._build_fields()
        self._fields_table: DataTable | None = None
        self._status_box: Static | None = None
        self._message_box: Static | None = None

    def _build_fields(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                key="kick_delta_v",
                label="Kick detuning delta (Enter apply)",
                unit="V",
                step=0.005,
                min_value=-1.2,
                max_value=1.2,
                precision=4,
                setter=lambda svc, value: svc.cli_kick(float(value)),
            ),
            FieldSpec(
                key="step_disturbance_v",
                label="Step disturbance delta (Enter apply)",
                unit="V",
                step=0.005,
                min_value=-1.2,
                max_value=1.2,
                precision=4,
                setter=lambda svc, value: svc.cli_step_disturbance(float(value)),
            ),
            FieldSpec(
                key="noise_sigma_v",
                label="Electronics noise sigma",
                unit="V",
                step=0.0005,
                min_value=0.0,
                precision=5,
                setter=lambda svc, value: svc.cli_set_noise(float(value)),
            ),
            FieldSpec(
                key="detuning_jitter_v",
                label="Laser/cavity jitter sigma",
                unit="V",
                step=0.0001,
                min_value=0.0,
                precision=5,
                setter=lambda svc, value: svc.cli_set_detuning_jitter(float(value)),
            ),
            FieldSpec(
                key="drift_v_per_s",
                label="Detuning drift",
                unit="V/s",
                step=0.0005,
                precision=5,
                setter=lambda svc, value: svc.cli_set_drift(float(value)),
            ),
            FieldSpec(
                key="walk_sigma_v_sqrt_s",
                label="Random walk sigma",
                unit="V/sqrt(s)",
                step=0.0005,
                min_value=0.0,
                precision=5,
                setter=lambda svc, value: svc.cli_set_walk(float(value)),
            ),
            FieldSpec(
                key="monitor_mode",
                label="Monitor mode",
                unit="",
                setter=lambda svc, value: svc.cli_set_monitor_mode(str(value)),
                choices=("reflection", "transmission"),
            ),
            FieldSpec(
                key="modulation_hz",
                label="Modulation frequency",
                unit="Hz",
                step=100_000.0,
                min_value=0.0,
                precision=0,
                setter=lambda svc, value: svc.cli_set_modfreq_hz(float(value)),
            ),
            FieldSpec(
                key="modulation_amp_vpp",
                label="Modulation amplitude",
                unit="Vpp",
                step=0.01,
                min_value=0.0,
                precision=4,
                setter=lambda svc, value: svc.cli_set_modamp_vpp(float(value)),
            ),
            FieldSpec(
                key="phase_active_deg",
                label="Demod phase (active channel)",
                unit="deg",
                step=1.0,
                precision=2,
                setter=lambda svc, value: svc.cli_set_phase_deg(
                    float(value), channel="active"
                ),
            ),
            FieldSpec(
                key="phase_a_deg",
                label="Demod phase A",
                unit="deg",
                step=1.0,
                precision=2,
                setter=lambda svc, value: svc.cli_set_phase_deg(float(value), channel="a"),
            ),
            FieldSpec(
                key="phase_b_deg",
                label="Demod phase B",
                unit="deg",
                step=1.0,
                precision=2,
                setter=lambda svc, value: svc.cli_set_phase_deg(float(value), channel="b"),
            ),
            FieldSpec(
                key="pid_p",
                label="PID P",
                unit="",
                step=0.5,
                precision=3,
                setter=lambda svc, value: svc.cli_set_pid_p(float(value)),
            ),
            FieldSpec(
                key="pid_i",
                label="PID I",
                unit="",
                step=5.0,
                precision=3,
                setter=lambda svc, value: svc.cli_set_pid_i(float(value)),
            ),
            FieldSpec(
                key="pid_d",
                label="PID D",
                unit="",
                step=0.1,
                precision=3,
                setter=lambda svc, value: svc.cli_set_pid_d(float(value)),
            ),
            FieldSpec(
                key="linewidth_hz",
                label="Linewidth",
                unit="Hz",
                step=10_000.0,
                min_value=100.0,
                precision=0,
                setter=lambda svc, value: svc.cli_set_linewidth_hz(float(value)),
            ),
            FieldSpec(
                key="linewidth_v",
                label="Linewidth scale",
                unit="V",
                step=0.0005,
                min_value=0.00001,
                precision=5,
                setter=lambda svc, value: svc.cli_set_linewidth_v(float(value)),
            ),
            FieldSpec(
                key="fsr_hz",
                label="Cavity FSR",
                unit="Hz",
                step=1_000_000.0,
                min_value=1000.0,
                precision=0,
                setter=lambda svc, value: svc.cli_set_fsr_hz(float(value)),
            ),
            FieldSpec(
                key="control_channel",
                label="Control channel",
                unit="",
                precision=0,
                setter=None,
            ),
        ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-layout"):
            with Vertical(id="fields-pane"):
                yield Static(
                    "Editable parameters (click row, use left/right, Enter for exact value)"
                )
                yield DataTable(id="fields-table")
            with Vertical(id="status-pane"):
                yield Static("Runtime status")
                yield Static("", id="status-box")
                yield Static(
                    "Keys: \u2190/\u2192 adjust, Enter edit, L lock, S sweep, Q quit",
                    id="message-box",
                )
        yield Footer()

    def on_mount(self) -> None:
        self._fields_table = self.query_one("#fields-table", DataTable)
        self._status_box = self.query_one("#status-box", Static)
        self._message_box = self.query_one("#message-box", Static)

        table = self._fields_table
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Parameter", key="param")
        table.add_column("Value", key="value")
        table.add_column("Unit", key="unit")
        table.add_column("Step", key="step")
        for field in self._fields:
            step_label = (
                "toggle"
                if field.choices
                else f"{field.step:.6g}" if field.step is not None else "-"
            )
            table.add_row(field.label, "-", field.unit or "-", step_label, key=field.key)

        table.focus()
        self._refresh_all()
        self.set_interval(0.2, self._refresh_all)

    def _refresh_all(self) -> None:
        self._refresh_fields()
        self._refresh_status()

    def _refresh_fields(self) -> None:
        if self._fields_table is None:
            return
        try:
            values = self._service.cli_get_tunables()
        except Exception as exc:  # pragma: no cover - runtime safeguard
            self._set_message(f"Read error: {exc}")
            return
        for field in self._fields:
            raw = (
                self._command_values[field.key]
                if field.key in self._command_values
                else values.get(field.key)
            )
            formatted = self._format_field_value(field, raw)
            try:
                self._fields_table.update_cell(field.key, "value", formatted)
            except Exception:
                continue

    def _refresh_status(self) -> None:
        if self._status_box is None:
            return
        try:
            status = self._service.cli_status()
        except Exception as exc:  # pragma: no cover - runtime safeguard
            self._set_message(f"Status error: {exc}")
            return
        self._status_box.update(self._format_status(status))

    @staticmethod
    def _format_status(status: SimulatorStatus) -> str:
        mode = "locked" if status.lock else "sweep"
        return (
            f"mode={mode}\n"
            f"detuning={status.laser_detuning_v:+.4f} V\n"
            f"disturbance={status.disturbance_offset_v:+.4f} V\n"
            f"effective={status.effective_detuning_v:+.4f} V\n"
            f"control={status.control_output_v:+.4f} V\n\n"
            f"noise={status.noise_sigma_v:.5f} V\n"
            f"jitter={status.detuning_jitter_v:.5f} V\n"
            f"drift={status.drift_v_per_s:+.5f} V/s\n"
            f"walk={status.walk_sigma_v_sqrt_s:.5f} V/sqrt(s)\n"
            f"monitor={status.monitor_mode}\n\n"
            f"modulation={status.modulation_hz / 1_000_000.0:.4f} MHz\n"
            f"amplitude={status.modulation_amp_vpp:.4f} Vpp\n"
            f"linewidth={status.linewidth_hz / 1_000_000.0:.4f} MHz "
            f"({status.linewidth_v:.5f} V)\n"
            f"fsr={status.fsr_hz / 1_000_000.0:.4f} MHz\n"
            f"scan={status.scan_hz_per_v / 1_000_000.0:.4f} MHz/V"
        )

    @staticmethod
    def _format_field_value(field: FieldSpec, value: Any) -> str:
        if value is None:
            return "-"
        if field.choices:
            return str(value)
        if isinstance(value, (int, float)):
            if field.precision <= 0:
                return f"{value:.0f}"
            return f"{float(value):.{field.precision}f}"
        return str(value)

    def _selected_field(self) -> FieldSpec | None:
        if self._fields_table is None:
            return None
        row_idx = self._fields_table.cursor_row
        if row_idx < 0 or row_idx >= len(self._fields):
            return None
        return self._fields[row_idx]

    def _set_message(self, message: str) -> None:
        if self._message_box is not None:
            self._message_box.update(message)

    def _apply_field_value(self, field: FieldSpec, value: Any) -> bool:
        if field.key in self._command_values:
            try:
                numeric = float(value)
                if field.min_value is not None:
                    numeric = max(field.min_value, numeric)
                if field.max_value is not None:
                    numeric = min(field.max_value, numeric)
            except Exception as exc:
                self._set_message(f"Update failed: {exc}")
                return False
            self._command_values[field.key] = numeric
            self._set_message(f"Set {field.label} to {numeric:+.4f} V.")
            self._refresh_fields()
            return True

        if field.setter is None:
            self._set_message(f"{field.label} is read-only.")
            return False
        try:
            if field.choices:
                if str(value) not in field.choices:
                    raise ValueError(
                        f"Expected one of: {', '.join(field.choices)}"
                    )
                field.setter(self._service, str(value))
            else:
                numeric = float(value)
                if field.min_value is not None:
                    numeric = max(field.min_value, numeric)
                if field.max_value is not None:
                    numeric = min(field.max_value, numeric)
                field.setter(self._service, numeric)
        except Exception as exc:
            self._set_message(f"Update failed: {exc}")
            return False
        self._set_message(f"Updated {field.label}.")
        self._refresh_all()
        return True

    def _step_selected(self, direction: int) -> None:
        field = self._selected_field()
        if field is None:
            return
        if field.key in self._command_values:
            if field.step is None:
                return
            current_num = float(self._command_values[field.key])
            next_value = current_num + (field.step * direction)
            if field.min_value is not None:
                next_value = max(field.min_value, next_value)
            if field.max_value is not None:
                next_value = min(field.max_value, next_value)
            self._command_values[field.key] = next_value
            self._set_message(f"Set {field.label} to {next_value:+.4f} V.")
            self._refresh_fields()
            return

        if field.setter is None:
            self._set_message(f"{field.label} is read-only.")
            return
        try:
            values = self._service.cli_get_tunables()
            current = values.get(field.key)
            if field.choices:
                if current not in field.choices:
                    current = field.choices[0]
                current_idx = field.choices.index(str(current))
                next_idx = (current_idx + direction) % len(field.choices)
                next_value = field.choices[next_idx]
            else:
                if field.step is None:
                    return
                current_num = float(current if current is not None else 0.0)
                next_value = current_num + (field.step * direction)
                if field.min_value is not None:
                    next_value = max(field.min_value, next_value)
                if field.max_value is not None:
                    next_value = min(field.max_value, next_value)
        except Exception as exc:
            self._set_message(f"Read failed: {exc}")
            return
        self._apply_field_value(field, next_value)

    def action_decrement(self) -> None:
        self._step_selected(-1)

    def action_increment(self) -> None:
        self._step_selected(+1)

    def action_edit_selected(self) -> None:
        field = self._selected_field()
        if field is None:
            return
        if field.key in self._command_values:
            if field.setter is None:
                self._set_message(f"{field.label} is read-only.")
                return
            value = float(self._command_values[field.key])
            try:
                field.setter(self._service, value)
            except Exception as exc:
                self._set_message(f"Apply failed: {exc}")
                return
            if field.key == "kick_delta_v":
                self._set_message(f"Applied detuning kick: {value:+.4f} V.")
            elif field.key == "step_disturbance_v":
                self._set_message(f"Applied disturbance step: {value:+.4f} V.")
            else:
                self._set_message(f"Applied {field.label}.")
            self._refresh_all()
            return
        if field.setter is None:
            self._set_message(f"{field.label} is read-only.")
            return
        try:
            values = self._service.cli_get_tunables()
            current = values.get(field.key)
        except Exception as exc:
            self._set_message(f"Read failed: {exc}")
            return

        if field.choices:
            self._step_selected(+1)
            return

        self.push_screen(
            ValueEditScreen(
                field.label,
                self._format_field_value(field, current),
            ),
            lambda result: self._apply_field_value(field, result)
            if result is not None and result != ""
            else None,
        )

    def action_lock_mode(self) -> None:
        try:
            self._service.exposed_start_lock()
            self._set_message("Lock mode started.")
            self._refresh_all()
        except Exception as exc:
            self._set_message(f"Lock command failed: {exc}")

    def action_sweep_mode(self) -> None:
        try:
            self._service.exposed_start_sweep()
            self._set_message("Sweep mode started.")
            self._refresh_all()
        except Exception as exc:
            self._set_message(f"Sweep command failed: {exc}")


def run_tui(service: VirtualLinienControlService) -> None:
    app = LinienSimTui(service)
    app.run()
