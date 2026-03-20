from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator

MHZ_UNIT = 0x10000000 / 8
VPP_UNIT = ((1 << 14) - 1) / 4


class Parameter:
    def __init__(
        self,
        *,
        min_: Any = None,
        max_: Any = None,
        start: Any = None,
        wrap: bool = False,
        sync: bool = True,
        collapsed_sync: bool = True,
        restorable: bool = False,
        loggable: bool = False,
        log: bool = False,
    ) -> None:
        self.min = min_
        self.max = max_
        self.wrap = wrap
        self._value = start
        self._start = start
        self._callbacks: set[Callable[[Any], None]] = set()
        self.can_be_cached = sync
        self._collapsed_sync = collapsed_sync
        self.restorable = restorable
        self.loggable = loggable
        self.log = log

    @property
    def value(self) -> Any:
        return self._value

    @value.setter
    def value(self, value: Any) -> None:
        if self.min is not None and value < self.min:
            value = self.max if self.wrap and self.max is not None else self.min
        if self.max is not None and value > self.max:
            value = self.min if self.wrap and self.min is not None else self.max
        self._value = value
        for callback in self._callbacks.copy():
            callback(value)

    def reset(self) -> None:
        self.value = self._start

    def add_callback(
        self, function: Callable[[Any], None], call_immediately: bool = False
    ) -> None:
        self._callbacks.add(function)
        if call_immediately and self._value is not None:
            function(self._value)

    def remove_callback(self, function: Callable[[Any], None]) -> None:
        if function in self._callbacks:
            self._callbacks.remove(function)


@dataclass
class _RemoteListener:
    param: Parameter
    callback: Callable[[Any], None]


class SimParameters:
    def __init__(self) -> None:
        self._changed_parameters_queue: dict[str, list[tuple[str, Any]]] = {}
        self._remote_listener_callbacks: dict[str, dict[str, _RemoteListener]] = {}
        self._lock = threading.RLock()

        self.to_plot = Parameter(sync=False)
        self.signal_stats = Parameter(sync=False, loggable=True)
        self.acquisition_raw_data = Parameter(sync=False, start=None)
        self.psd_data_partial = Parameter(sync=False, start=None)
        self.psd_data_complete = Parameter(sync=False, start=None)
        self.control_signal_history = Parameter(
            sync=False,
            start={"times": [], "values": [], "slow_times": [], "slow_values": []},
        )
        self.monitor_signal_history = Parameter(
            sync=False, start={"times": [], "values": []}
        )
        self.task = Parameter(sync=False, start=None)

        self.mod_channel = Parameter(start=0, min_=0, max_=1, restorable=True)
        self.sweep_channel = Parameter(start=1, min_=0, max_=2, restorable=True)
        self.control_channel = Parameter(start=0, min_=0, max_=1, restorable=True)
        self.slow_control_channel = Parameter(start=2, min_=0, max_=2, restorable=True)

        self.analog_out_1 = Parameter(start=0, min_=0, max_=(2**15) - 1, restorable=True)
        self.analog_out_2 = Parameter(start=0, min_=0, max_=(2**15) - 1, restorable=True)
        self.analog_out_3 = Parameter(start=0, min_=0, max_=(2**15) - 1, restorable=True)

        self.lock = Parameter(start=False, loggable=True)
        self.polarity_fast_out1 = Parameter(start=False, restorable=True)
        self.polarity_fast_out2 = Parameter(start=False, restorable=True)
        self.polarity_analog_out0 = Parameter(start=False, restorable=True)
        self.pid_only_mode = Parameter(start=False, restorable=True)
        self.dual_channel = Parameter(start=False, restorable=True)
        self.channel_mixing = Parameter(start=0, restorable=True, loggable=True)

        self.control_signal_history_length = Parameter(start=600)
        self.pause_acquisition = Parameter(start=False)
        self.fetch_additional_signals = Parameter(start=True)
        self.ping = Parameter(start=0)

        self.sweep_amplitude = Parameter(min_=0.001, max_=1, start=1, loggable=True)
        self.sweep_center = Parameter(min_=-1, max_=1, start=0, loggable=True)
        self.sweep_speed = Parameter(
            min_=0, max_=15, start=8, restorable=True, loggable=True
        )
        self.sweep_pause = Parameter(start=False, loggable=True)

        self.modulation_amplitude = Parameter(
            min_=0,
            max_=(1 << 14) - 1,
            start=int(1 * VPP_UNIT),
            restorable=True,
            loggable=True,
        )
        self.modulation_frequency = Parameter(
            min_=0,
            max_=0xFFFFFFFF,
            start=int(1 * MHZ_UNIT),
            restorable=True,
            loggable=True,
        )

        self.demodulation_phase_a = Parameter(
            min_=0, max_=360, start=100, wrap=True, restorable=True, loggable=True
        )
        self.demodulation_phase_b = Parameter(
            min_=0, max_=360, start=100, wrap=True, restorable=True, loggable=True
        )
        self.demodulation_multiplier_a = Parameter(
            min_=1, max_=15, start=1, restorable=True, loggable=True
        )
        self.demodulation_multiplier_b = Parameter(
            min_=1, max_=15, start=1, restorable=True, loggable=True
        )
        self.offset_a = Parameter(
            min_=-8191, max_=8191, start=0, restorable=True, loggable=True
        )
        self.offset_b = Parameter(
            min_=-8191, max_=8191, start=0, restorable=True, loggable=True
        )
        self.invert_a = Parameter(start=False, restorable=True)
        self.invert_b = Parameter(start=False, restorable=True)

        self.filter_automatic_a = Parameter(start=2, restorable=True)
        self.filter_automatic_b = Parameter(start=2, restorable=True)
        self.filter_1_enabled_a = Parameter(start=False, restorable=True)
        self.filter_1_enabled_b = Parameter(start=False, restorable=True)
        self.filter_2_enabled_a = Parameter(start=False, restorable=True)
        self.filter_2_enabled_b = Parameter(start=False, restorable=True)
        self.filter_1_frequency_a = Parameter(start=10000, restorable=True)
        self.filter_1_frequency_b = Parameter(start=10000, restorable=True)
        self.filter_2_frequency_a = Parameter(start=10000, restorable=True)
        self.filter_2_frequency_b = Parameter(start=10000, restorable=True)
        self.filter_1_type_a = Parameter(start=0, restorable=True)
        self.filter_1_type_b = Parameter(start=0, restorable=True)
        self.filter_2_type_a = Parameter(start=0, restorable=True)
        self.filter_2_type_b = Parameter(start=0, restorable=True)

        self.combined_offset = Parameter(min_=-8191, max_=8191, start=0)
        self.p = Parameter(start=10, max_=8191, restorable=True, loggable=True)
        self.i = Parameter(start=500, max_=8191, restorable=True, loggable=True)
        self.d = Parameter(start=0, max_=8191, restorable=True, loggable=True)
        self.target_slope_rising = Parameter(start=True)
        self.pid_on_slow_enabled = Parameter(start=False, restorable=True)
        self.pid_on_slow_strength = Parameter(start=0, restorable=True)
        self.check_lock = Parameter(start=True, restorable=True)
        self.watch_lock = Parameter(start=True, restorable=True)
        self.watch_lock_threshold = Parameter(start=0.01, restorable=True)

        self.automatic_mode = Parameter(start=False)
        self.autolock_target_position = Parameter(start=0)
        self.autolock_mode_preference = Parameter(start=0, restorable=True)
        self.autolock_mode = Parameter(start=0)
        self.autolock_time_scale = Parameter(start=0)
        self.autolock_instructions = Parameter(start=[], sync=False)
        self.autolock_final_wait_time = Parameter(start=0)
        self.autolock_selection = Parameter(start=False)
        self.autolock_running = Parameter(start=False)
        self.autolock_preparing = Parameter(start=False)
        self.autolock_percentage = Parameter(start=0, min_=0, max_=100)
        self.autolock_watching = Parameter(start=False)
        self.autolock_failed = Parameter(start=False)
        self.autolock_locked = Parameter(start=False)
        self.autolock_retrying = Parameter(start=False)
        self.autolock_determine_offset = Parameter(start=True, restorable=True)
        self.autolock_initial_sweep_amplitude = Parameter(start=1)

        self.optimization_selection = Parameter(start=False)
        self.optimization_running = Parameter(start=False)
        self.optimization_approaching = Parameter(start=False)
        self.optimization_improvement = Parameter(start=0)
        self.optimization_mod_freq_enabled = Parameter(start=1)
        self.optimization_mod_freq_min = Parameter(start=0.0)
        self.optimization_mod_freq_max = Parameter(start=10.0)
        self.optimization_mod_amp_enabled = Parameter(start=1)
        self.optimization_mod_amp_min = Parameter(start=0.0)
        self.optimization_mod_amp_max = Parameter(start=2.0)
        self.optimization_optimized_parameters = Parameter(start=(0, 0, 0))
        self.optimization_channel = Parameter(start=0)
        self.optimization_failed = Parameter(start=False)

        self.acquisition_raw_enabled = Parameter(start=False)
        self.acquisition_raw_decimation = Parameter(start=1)
        self.acquisition_raw_filter_enabled = Parameter(start=False)
        self.acquisition_raw_filter_frequency = Parameter(start=0)
        self.psd_algorithm = Parameter(start=0, restorable=True)
        self.psd_acquisition_running = Parameter(start=False)
        self.psd_optimization_running = Parameter(start=False)
        self.psd_acquisition_max_decimation = Parameter(
            start=18, min_=1, max_=32, restorable=True
        )

    def __iter__(self) -> Iterator[tuple[str, Parameter]]:
        for name, param in self.__dict__.items():
            if isinstance(param, Parameter):
                yield name, param

    def init_parameter_sync(
        self, uuid: str
    ) -> Iterator[tuple[str, Any, bool, bool, bool, bool]]:
        for name, param in self:
            yield (
                name,
                param.value,
                param.can_be_cached,
                param.restorable,
                param.loggable,
                param.log,
            )
            if param.can_be_cached:
                self.register_remote_listener(uuid, name)

    def register_remote_listener(self, uuid: str, param_name: str) -> None:
        with self._lock:
            self._changed_parameters_queue.setdefault(uuid, [])
            per_uuid = self._remote_listener_callbacks.setdefault(uuid, {})
            if param_name in per_uuid:
                return

            def append_changed_values_to_queue(value: Any) -> None:
                with self._lock:
                    if uuid in self._changed_parameters_queue:
                        self._changed_parameters_queue[uuid].append((param_name, value))

            param: Parameter = getattr(self, param_name)
            param.add_callback(append_changed_values_to_queue, call_immediately=True)
            per_uuid[param_name] = _RemoteListener(param=param, callback=append_changed_values_to_queue)

    def register_remote_listeners(self, uuid: str, param_names: list[str]) -> None:
        for name in param_names:
            self.register_remote_listener(uuid, name)

    def unregister_remote_listeners(self, uuid: str) -> None:
        with self._lock:
            per_uuid = self._remote_listener_callbacks.pop(uuid, {})
            for item in per_uuid.values():
                item.param.remove_callback(item.callback)
            self._changed_parameters_queue.pop(uuid, None)

    def get_changed_parameters_queue(self, uuid: str) -> list[tuple[str, Any]]:
        with self._lock:
            queue = self._changed_parameters_queue.get(uuid, [])[:]
            self._changed_parameters_queue[uuid] = []

        already_has_value: set[str] = set()
        for idx in reversed(range(len(queue))):
            param_name, _value = queue[idx]
            if getattr(self, param_name)._collapsed_sync:
                if param_name in already_has_value:
                    del queue[idx]
                else:
                    already_has_value.add(param_name)
        return queue
