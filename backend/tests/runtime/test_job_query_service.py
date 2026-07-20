from __future__ import annotations

from pathlib import Path

from app.adapters.jobs.sqlite_repository import SqliteJobRepository
from app.core.application.job_query_service import JobQueryService
from app.core.domain.video import JobState


def test_ten_thousand_job_cursor_walk_is_bounded_complete_and_stable(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine")
    with repository._transaction(immediate=True) as connection:
        connection.executemany(
            """
            INSERT INTO jobs (
                job_id, request_hash, request_json, principal,
                client_request_id, state, cancellation_requested,
                retry_of_job_id, result_json, error_json, created_at, updated_at
            ) VALUES (?, ?, NULL, 'local-user', ?, 'queued', 0,
                      NULL, NULL, NULL, ?, ?)
            """,
            (
                (
                    f"job_fixture_{index:05d}",
                    "sha256:" + f"{index:064x}",
                    f"request-{index:05d}",
                    f"{index:010d}",
                    f"{index:010d}",
                )
                for index in range(10_000)
            ),
        )

    service = JobQueryService(repository)
    cursor: str | None = None
    observed: list[str] = []
    while True:
        page = service.list(
            principal="local-user",
            states=(JobState.QUEUED,),
            cursor=cursor,
            limit=200,
        )
        assert 1 <= len(page.jobs) <= 200
        observed.extend(view.snapshot.job_id for view in page.jobs)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(observed) == 10_000
    assert len(set(observed)) == 10_000
    assert observed[0] == "job_fixture_09999"
    assert observed[-1] == "job_fixture_00000"


def test_job_event_page_uses_limit_plus_one_without_loading_the_tail(
    tmp_path: Path,
) -> None:
    repository = SqliteJobRepository.open(tmp_path / "machine")
    job = repository.create_job(
        request_hash="sha256:" + "a" * 64,
        principal="local-user",
        client_request_id="events",
    )
    for index in range(5):
        repository.append_event(
            job.job_id,
            "fixture.event",
            f'{{"index":{index}}}',
        )

    page = JobQueryService(repository).events(
        job.job_id,
        principal="local-user",
        after_sequence=1,
        limit=2,
    )

    assert [event.sequence for event in page.events] == [2, 3]
    assert page.next_after_sequence == 3
