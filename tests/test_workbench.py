from pathlib import Path

from video_agent.api import STATIC_DIR, favicon, workbench


def test_workbench_is_served_without_starting_dependencies():
    response = workbench()
    html = Path(response.path).read_text("utf-8")
    assert response.status_code == 200
    assert Path(response.path) == STATIC_DIR / "index.html"
    assert "证据台" in html
    assert "/static/app.js" in html


def test_workbench_does_not_embed_api_key():
    static = Path(__file__).parents[1] / "video_agent" / "static"
    combined = "\n".join(path.read_text("utf-8") for path in static.iterdir())
    assert "demo-key" not in combined
    assert 'sessionStorage.getItem("apiKey")' in combined


def test_favicon_exists():
    response = favicon()
    assert response.media_type == "image/svg+xml"
    assert Path(response.path).is_file()
