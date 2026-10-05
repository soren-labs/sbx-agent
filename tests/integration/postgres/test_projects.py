"""Project + immutable ProjectVersion lifecycle (RFC 167 §02/§06):
publish → CAS current pointer → environment key → slug conflicts."""

import pytest
from control.application.projects import ProjectService
from control.domain.errors import DomainError
from control.persistence.unit_of_work import SqlUnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture()
def svc(pg):
    return ProjectService(pg)


class TestProjectVersions:
    def test_publish_pins_digest_and_pointer(self, pg, svc, workspace):
        ws = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            project = svc.create_project(uow, workspace_id=ws, slug="api", name="API")
            v1 = svc.publish_version(
                uow,
                workspace_id=ws,
                project_id=project["id"],
                repository="soren-labs/sbx-e2e-test",
                base_ref="main",
                environment={"base_image": "sbx-runtime:1"},
                services=[
                    {"name": "web", "argv": ["python", "-m", "http.server"], "preview": True}
                ],
                defaults={"model": "opencode/big-pickle", "executor_backend": "modal"},
                created_by=workspace["user_id"],
            )
        assert v1["spec_digest"].startswith("sha256:")
        assert v1["ordinal"] == 1
        with SqlUnitOfWork(pg) as uow:
            cur = svc.current_version(uow, workspace_id=ws, project_id=project["id"])
            assert cur["id"] == v1["id"]
            assert cur["environment"]["base_image"] == "sbx-runtime:1"
            assert cur["services"][0]["name"] == "web"

    def test_second_version_keeps_first_immutable(self, pg, svc, workspace):
        ws = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            project = svc.create_project(uow, workspace_id=ws, slug="api", name="API")
            v1 = svc.publish_version(
                uow, workspace_id=ws, project_id=project["id"], repository="a/b"
            )
            v2 = svc.publish_version(
                uow,
                workspace_id=ws,
                project_id=project["id"],
                repository="a/b",
                defaults={"model": "opencode/other"},
            )
        assert v1["spec_digest"] != v2["spec_digest"]
        with SqlUnitOfWork(pg) as uow:
            back = svc.get_version(uow, workspace_id=ws, project_version_id=v1["id"])
            # Untouched by v2: only dataclass defaults persisted.
            assert back["defaults"] == {
                "executor_backend": "modal",
                "resource_class": "standard",
            }

    def test_slug_conflict_404_scope(self, pg, svc, workspace):
        ws = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            svc.create_project(uow, workspace_id=ws, slug="api", name="API")
            with pytest.raises(DomainError) as ei:
                svc.create_project(uow, workspace_id=ws, slug="api", name="Again")
            assert ei.value.code == "idempotency_conflict"
            with pytest.raises(DomainError) as ei2:
                svc.get_project(uow, workspace_id="wsp_other", project_id_or_slug="api")
            assert ei2.value.code == "not_found"

    def test_bad_repository_rejected(self, pg, svc, workspace):
        ws = workspace["workspace_id"]
        with SqlUnitOfWork(pg) as uow:
            project = svc.create_project(uow, workspace_id=ws, slug="api", name="API")
            with pytest.raises(DomainError):
                svc.publish_version(
                    uow,
                    workspace_id=ws,
                    project_id=project["id"],
                    repository="https://evil/token@repo",
                )
