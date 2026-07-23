from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "compose.example.yaml"


def _service_block(name: str) -> str:
    lines = COMPOSE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {name}:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("  ") and not lines[i].startswith("    ") and lines[i].endswith(":"):
            end = i
            break
    return "\n".join(lines[start:end])


def _top_level_block(name: str) -> str:
    lines = COMPOSE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"{name}:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i].startswith(" ") and lines[i].endswith(":"):
            end = i
            break
    return "\n".join(lines[start:end])


def test_public_api_remains_externally_consumable_service():
    api = _service_block("afl-api")

    assert '"${AFL_API_PORT:-8000}:8000"' in api
    assert "command: uvicorn main:app --host 0.0.0.0 --port 8000 --reload" in api


def test_admin_remains_authenticated_management_interface_on_configured_port():
    admin = _service_block("afl-admin")
    admin_source = (Path(__file__).resolve().parents[1] / "admin.py").read_text()

    assert '"${AFL_ADMIN_PORT:-8001}:8001"' in admin
    assert "127.0.0.1:${AFL_ADMIN_PORT" not in admin
    assert "security = HTTPBasic()" in admin_source
    assert "dependencies=[Depends(verify_admin)]" in admin_source
    assert "- management" in admin
    assert "- admin-access" in admin


def test_scheduler_has_no_published_host_ports_for_mutation_endpoints():
    scheduler = _service_block("afl-scheduler")

    assert "ports:" not in scheduler
    assert "expose:" not in scheduler
    assert "command: uvicorn scheduler.start:app --host 0.0.0.0 --port 8000" in scheduler
    assert "depends_on:" not in scheduler


def test_admin_to_scheduler_uses_internal_management_network():
    admin = _service_block("afl-admin")
    scheduler = _service_block("afl-scheduler")
    admin_source = (Path(__file__).resolve().parents[1] / "admin.py").read_text()

    assert "- management" in admin
    assert "- management" in scheduler
    assert "http://afl-scheduler:8000/scheduler/jobs" in admin_source
    assert "http://afl-scheduler:8000/scheduler/refresh" in admin_source


def test_network_topology_keeps_scheduler_off_default_but_preserves_egress():
    admin = _service_block("afl-admin")
    scheduler = _service_block("afl-scheduler")
    networks = _top_level_block("networks")

    assert "- default" not in admin
    assert "- default" not in scheduler
    assert "- admin-access" not in scheduler
    assert "- scheduler-egress" in scheduler
    assert "  management:" in networks
    assert "    internal: true" in networks
    assert "  admin-access:" in networks
    assert "  scheduler-egress:" in networks
