import ast
from pathlib import Path

from video_agent.application import Services
from video_agent.config import Settings


def test_api_lifespan_does_not_start_periodic_tasks():
    tree = ast.parse(Path("video_agent/api.py").read_text(encoding="utf-8"))
    lifespan = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "lifespan")
    calls = {getattr(node.func, "attr", "") for node in ast.walk(lifespan)
             if isinstance(node, ast.Call)}
    assert "create_task" not in calls


def test_services_accept_fake_external_dependencies():
    fake = object()
    services = Services(Settings(_env_file=None), lambda: fake, fake, fake, fake, fake, fake)
    assert services.objects is fake
    assert services.workflows is fake
    assert services.model is fake


def test_capability_routes_are_physically_split_from_composition_root():
    expected = {"upload.py", "video.py", "conversation.py", "analysis.py", "admin.py"}
    route_dir = Path("video_agent/routers")
    assert expected <= {path.name for path in route_dir.glob("*.py")}

    api_tree = ast.parse(Path("video_agent/api.py").read_text(encoding="utf-8"))
    route_decorators = []
    for node in ast.walk(api_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            route_decorators.extend(
                decorator for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
            )
    assert route_decorators == []
