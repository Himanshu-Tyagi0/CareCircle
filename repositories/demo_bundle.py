"""Factory helper assembling an isolated DemoStore into a Repositories bundle."""

from __future__ import annotations

from repositories.demo_auth_repo import DemoAuthRepository, DemoCircleRepository
from repositories.demo_clinical_repo import DemoDoseRepository, DemoMedicationRepository
from repositories.demo_collab_repo import DemoCollabRepository
from repositories.demo_outbox_repo import DemoOutboxRepository
from repositories.demo_store import DemoStore
from repositories.interfaces import Repositories


def create_demo_repositories(store: DemoStore | None = None) -> tuple[DemoStore, Repositories]:
    """Build an isolated demo repository bundle sharing one thread-safe store."""
    target = store or DemoStore()
    repos = Repositories(
        auth=DemoAuthRepository(target),
        circle=DemoCircleRepository(target),
        medication=DemoMedicationRepository(target),
        dose=DemoDoseRepository(target),
        collab=DemoCollabRepository(target),
        outbox=DemoOutboxRepository(target),
    )
    return target, repos
