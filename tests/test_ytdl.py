"""Preset -> yt-dlp option mapping and extra-args merging."""

from __future__ import annotations

import pytest

from app import config, ytdl

VIDEO_PRESETS = ["best", "1080p", "720p", "480p"]
AUDIO_PRESETS = ["audio-mp3", "audio-m4a"]


def test_all_expected_presets_exist():
    assert set(ytdl.PRESETS) == set(VIDEO_PRESETS + AUDIO_PRESETS)
    assert ytdl.DEFAULT_PRESET == "best"
    assert ytdl.PRESET_NAMES[0] == "best"


@pytest.mark.parametrize("name", VIDEO_PRESETS)
def test_video_presets_merge_to_mp4(name):
    opts = ytdl.preset_options(name)
    assert opts["merge_output_format"] == "mp4"
    assert "format" in opts and opts["format"]
    assert "postprocessors" not in opts


@pytest.mark.parametrize("name,height", [("1080p", 1080), ("720p", 720), ("480p", 480)])
def test_height_capped_presets(name, height):
    assert f"height<={height}" in ytdl.preset_options(name)["format"]


def test_best_preset_is_uncapped():
    assert "height<=" not in ytdl.preset_options("best")["format"]


@pytest.mark.parametrize("name,codec", [("audio-mp3", "mp3"), ("audio-m4a", "m4a")])
def test_audio_presets_extract_audio(name, codec):
    opts = ytdl.preset_options(name)
    pps = opts["postprocessors"]
    assert pps[0]["key"] == "FFmpegExtractAudio"
    assert pps[0]["preferredcodec"] == codec
    assert "merge_output_format" not in opts


def test_unknown_preset_falls_back_to_best():
    assert ytdl.preset_options("nope") == ytdl.preset_options("best")


def test_preset_options_returns_a_copy():
    a = ytdl.preset_options("audio-mp3")
    a["postprocessors"][0]["preferredcodec"] = "tampered"
    assert ytdl.preset_options("audio-mp3")["postprocessors"][0]["preferredcodec"] == "mp3"


def test_build_opts_core_fields(tmp_path):
    target = tmp_path / "dest"
    target.mkdir()
    opts = ytdl.build_opts(preset="720p", target_dir=target)
    assert opts["continuedl"] is True
    assert opts["noprogress"] is True
    assert opts["ignoreerrors"] is False
    assert opts["outtmpl"]["default"].endswith("%(title)s [%(id)s].%(ext)s")
    assert str(target) in opts["outtmpl"]["default"]
    # Single-URL jobs never use the archive; only playlist children opt in.
    assert "download_archive" not in opts
    opts = ytdl.build_opts(preset="720p", target_dir=target, use_archive=True)
    assert opts["download_archive"] == str(ytdl.archive_path_for(target))
    assert opts["download_archive"].endswith(".txt")
    other = tmp_path / "other"
    other.mkdir()
    assert ytdl.archive_path_for(other) != ytdl.archive_path_for(target)


def test_build_opts_cookiefile_only_when_present(tmp_path, monkeypatch):
    opts = ytdl.build_opts(preset="best", target_dir=tmp_path)
    assert ("cookiefile" in opts) == config.COOKIES_FILE.is_file()

    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(config, "COOKIES_FILE", cookies)
    opts = ytdl.build_opts(preset="best", target_dir=tmp_path)
    assert opts["cookiefile"] == str(cookies)


def test_build_opts_flat_mode(tmp_path):
    opts = ytdl.build_opts(preset="best", target_dir=tmp_path, flat=True)
    assert opts["extract_flat"] == "in_playlist"
    assert opts["skip_download"] is True
    assert opts["noplaylist"] is False
    assert "download_archive" not in opts


def test_parse_extra_args_empty():
    assert ytdl.parse_extra_args("") == {}
    assert ytdl.parse_extra_args(None) == {}
    assert ytdl.parse_extra_args("   ") == {}


def test_parse_extra_args_only_returns_changed_keys():
    parsed = ytdl.parse_extra_args("--limit-rate 50K --write-subs")
    assert parsed["ratelimit"] == 51200
    assert parsed["writesubtitles"] is True
    # It must not drag in yt-dlp's whole default option set.
    assert len(parsed) < 10
    assert "outtmpl" not in parsed


def test_parse_extra_args_does_not_clobber_preset(tmp_path):
    opts = ytdl.build_opts(
        preset="720p", target_dir=tmp_path, extra_args="--limit-rate 1M"
    )
    assert "height<=720" in opts["format"]
    assert opts["outtmpl"]["default"].endswith("%(title)s [%(id)s].%(ext)s")
    assert opts["ratelimit"] == 1024 * 1024


def test_parse_extra_args_can_override_format(tmp_path):
    opts = ytdl.build_opts(preset="720p", target_dir=tmp_path, extra_args="-f worst")
    assert opts["format"] == "worst"


@pytest.mark.parametrize(
    "args",
    [
        "-P /etc",
        "--paths /etc",
        "-P home:/etc",
    ],
)
def test_extra_args_cannot_override_download_paths(tmp_path, args):
    opts = ytdl.build_opts(preset="best", target_dir=tmp_path, extra_args=args)
    assert opts["paths"] == {"home": str(tmp_path)}
    assert str(tmp_path) in opts["outtmpl"]["default"]


@pytest.mark.parametrize(
    "args",
    [
        "-o /etc/passwd.%(ext)s",
        "-o C:/Windows/evil.%(ext)s",
        "-o ../../escape/%(title)s.%(ext)s",
    ],
)
def test_extra_args_cannot_escape_via_outtmpl(tmp_path, args):
    opts = ytdl.build_opts(preset="best", target_dir=tmp_path, extra_args=args)
    default = opts["outtmpl"]["default"]
    assert default.startswith(str(tmp_path))
    assert ".." not in default.replace("\\", "/").split(str(tmp_path).replace("\\", "/"))[-1]


def test_extra_args_relative_outtmpl_is_rerooted(tmp_path):
    opts = ytdl.build_opts(
        preset="best", target_dir=tmp_path, extra_args="-o season1/%(title)s.%(ext)s"
    )
    default = opts["outtmpl"]["default"].replace("\\", "/")
    assert default.startswith(str(tmp_path).replace("\\", "/"))
    assert default.endswith("season1/%(title)s.%(ext)s")


def test_parse_extra_args_invalid_flag_raises():
    with pytest.raises(ytdl.ExtraArgsError):
        ytdl.parse_extra_args("--definitely-not-a-real-flag")


def test_parse_extra_args_unbalanced_quotes_raises():
    with pytest.raises(ytdl.ExtraArgsError):
        ytdl.parse_extra_args('--output "unclosed')


def test_ytdlp_version_is_reported():
    version = ytdl.ytdlp_version()
    assert version and version != "unknown"
