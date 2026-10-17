from pathlib import Path

from video_agent import migrations


def test_alembic_environment_and_baseline_are_packaged():
    root = Path(migrations.__file__).with_name("alembic_migrations")
    assert (root / "env.py").is_file()
    assert (root / "script.py.mako").is_file()
    baseline = root / "versions" / "0001_baseline.py"
    assert baseline.is_file()
    assert 'revision = "0001_baseline"' in baseline.read_text("utf-8")
