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
