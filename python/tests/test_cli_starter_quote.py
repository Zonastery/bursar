import json
from importlib.resources import files

import pytest

from bursar.__main__ import main
from bursar.config import load_config_from_dict
from bursar.load_config_file import load_config_text


def test_packaged_starter_and_offline_quote(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    main(["config", "init"])
    assert json.loads(capsys.readouterr().out)["created"] is True
    path = tmp_path / "bursar.yaml"
    original = path.read_text()
    assert original == files("bursar").joinpath("starter.yaml").read_text(encoding="utf-8")
    load_config_from_dict(load_config_text(original, is_yaml=True, source=str(path)))
    main(["config", "quote", str(path), "--operation", "export_report"])
    assert json.loads(capsys.readouterr().out)["total"] == "2.000000"
    with pytest.raises(SystemExit):
        main(["config", "init"])
    assert path.read_text() == original


def test_quote_invalid_usage_exits_cleanly(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(files("bursar").joinpath("starter.yaml").read_text(encoding="utf-8"))
    with pytest.raises(SystemExit):
        main(["config", "quote", str(path), "--operation", "export_report", "--measure", "bad"])
    assert "NAME=VALUE" in capsys.readouterr().err
