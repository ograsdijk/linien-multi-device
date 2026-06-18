import app.config as config


def test_plot_stream_config_defaults(monkeypatch):
    monkeypatch.setattr(config, "_load_config", lambda: {})

    assert config.get_plot_stream_default_fps() == config.DEFAULT_PLOT_STREAM_DEFAULT_FPS
    assert config.get_plot_stream_max_fps_cap() == config.DEFAULT_PLOT_STREAM_MAX_FPS_CAP
    assert (
        config.get_plot_stream_drop_old_frames()
        is config.DEFAULT_PLOT_STREAM_DROP_OLD_FRAMES
    )


def test_plot_stream_config_overrides(monkeypatch):
    monkeypatch.setattr(
        config,
        "_load_config",
        lambda: {
            "plotStreamDefaultFps": 22,
            "plotStreamMaxFpsCap": 44,
            "plotStreamDropOldFrames": False,
        },
    )

    assert config.get_plot_stream_default_fps() == 22
    assert config.get_plot_stream_max_fps_cap() == 44
    assert config.get_plot_stream_drop_old_frames() is False


def test_get_api_port_rejects_bool(monkeypatch):
    # bool is an int subclass; `true` must not be accepted as a port.
    monkeypatch.setattr(config, "_load_config", lambda: {"apiPort": True})
    assert config.get_api_port() == config.DEFAULT_API_PORT
    monkeypatch.setattr(config, "_load_config", lambda: {"apiPort": 9001})
    assert config.get_api_port() == 9001


def test_get_positive_float_rejects_bool(monkeypatch):
    monkeypatch.setattr(config, "_load_config", lambda: {"plotStreamDefaultFps": True})
    assert config.get_plot_stream_default_fps() == config.DEFAULT_PLOT_STREAM_DEFAULT_FPS


def test_load_config_tolerates_os_error(monkeypatch):
    import app.config as cfg

    class _BadPath:
        def __truediv__(self, _other):
            return self

        def exists(self):
            return True

        def read_text(self, *a, **k):
            raise OSError("permission denied")

    monkeypatch.setattr(cfg, "_repo_root", lambda: _BadPath())
    # Must not raise; falls back to empty config -> defaults.
    assert config.get_api_host() == config.DEFAULT_API_HOST


def test_main_entrypoint_binds_from_config(monkeypatch):
    import app.main as main

    captured = {}

    def fake_run(app_path, **kwargs):
        captured["host"] = kwargs.get("host")
        captured["port"] = kwargs.get("port")

    monkeypatch.setattr(main, "uvicorn", type("U", (), {"run": staticmethod(fake_run)}))
    monkeypatch.setattr(main, "get_api_host", lambda: "0.0.0.0")
    monkeypatch.setattr(main, "get_api_port", lambda: 9999)

    main.main()

    assert captured == {"host": "0.0.0.0", "port": 9999}
