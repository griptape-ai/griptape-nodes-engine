"""Deciding which id each workflow file is registered under."""

from __future__ import annotations

from pathlib import Path

from griptape_nodes.node_library.workflow_ids import (
    FoundWorkflowFile,
    RegisteredWorkflowFile,
    WorkflowIdReason,
    is_workflow_id,
    library_workflow_id,
    path_workflow_id,
    reconcile_workflow_ids,
)

HEADER_ID = "0123456789abcdef0123456789abcdef"
PEER_ID = "fedcba9876543210fedcba9876543210"
OTHER_ID = "00000000000000000000000000000abc"
ORIGINAL = Path("/workspace/original.py")
COPY = Path("/workspace/copy.py")


def _decide_one(file: FoundWorkflowFile, *registered: RegisteredWorkflowFile) -> tuple[str, WorkflowIdReason]:
    (decision,) = reconcile_workflow_ids([file], registered)
    return decision.workflow_id, decision.reason


class TestDerivedIds:
    def test_a_path_id_is_the_same_every_time_and_differs_by_path(self) -> None:
        assert path_workflow_id(ORIGINAL) == path_workflow_id(ORIGINAL)
        assert path_workflow_id(ORIGINAL) != path_workflow_id(COPY)
        assert is_workflow_id(path_workflow_id(ORIGINAL))

    def test_a_library_id_ignores_how_the_relative_path_is_spelled(self) -> None:
        assert library_workflow_id("Lib", "./flows/a.py") == library_workflow_id("Lib", "flows/a.py")
        assert library_workflow_id("Lib", "flows\\a.py") == library_workflow_id("Lib", "flows/a.py")
        assert library_workflow_id("Lib", "flows/a.py") != library_workflow_id("Other", "flows/a.py")


class TestANewFile:
    def test_takes_its_header_id(self) -> None:
        assert _decide_one(FoundWorkflowFile(ORIGINAL, header_id=HEADER_ID)) == (HEADER_ID, WorkflowIdReason.HEADER)

    def test_without_a_header_id_takes_its_path_id(self) -> None:
        assert _decide_one(FoundWorkflowFile(ORIGINAL)) == (path_workflow_id(ORIGINAL), WorkflowIdReason.PATH)

    def test_a_library_file_takes_its_library_id_over_its_header_id(self) -> None:
        library_id = library_workflow_id("Lib", "flow.py")

        decided = _decide_one(FoundWorkflowFile(ORIGINAL, header_id=HEADER_ID, library_id=library_id))

        assert decided == (library_id, WorkflowIdReason.LIBRARY)


class TestARegisteredFile:
    def test_keeps_its_entry_whatever_its_header_says(self) -> None:
        registered = RegisteredWorkflowFile(OTHER_ID, (ORIGINAL,), file_present=True)

        (decision,) = reconcile_workflow_ids([FoundWorkflowFile(ORIGINAL, header_id=HEADER_ID)], [registered])

        assert decision.workflow_id == OTHER_ID
        assert decision.reason is WorkflowIdReason.REGISTERED
        assert decision.entry_id == OTHER_ID


class TestTwoFilesWithOneId:
    def test_the_registered_file_keeps_it_and_the_copy_gets_its_path_id(self) -> None:
        registered = RegisteredWorkflowFile(HEADER_ID, (ORIGINAL,), file_present=True)

        decided = _decide_one(FoundWorkflowFile(COPY, header_id=HEADER_ID), registered)

        assert decided == (path_workflow_id(COPY), WorkflowIdReason.DUPLICATE)

    def test_found_together_the_first_by_path_keeps_it_whatever_order_they_were_found_in(self) -> None:
        copy = FoundWorkflowFile(COPY, header_id=HEADER_ID)
        original = FoundWorkflowFile(ORIGINAL, header_id=HEADER_ID)

        forwards = {
            decision.file.path: decision.workflow_id for decision in reconcile_workflow_ids([original, copy], [])
        }
        backwards = {
            decision.file.path: decision.workflow_id for decision in reconcile_workflow_ids([copy, original], [])
        }

        assert forwards == backwards == {COPY: HEADER_ID, ORIGINAL: path_workflow_id(ORIGINAL)}

    def test_a_header_id_an_unsaved_workflow_holds_is_a_duplicate(self) -> None:
        unsaved = RegisteredWorkflowFile(HEADER_ID, (), file_present=False)

        decided = _decide_one(FoundWorkflowFile(COPY, header_id=HEADER_ID), unsaved)

        assert decided == (path_workflow_id(COPY), WorkflowIdReason.DUPLICATE)

    def test_a_copy_whose_path_id_is_taken_too_gets_another_id(self) -> None:
        registered = [
            RegisteredWorkflowFile(HEADER_ID, (ORIGINAL,), file_present=True),
            RegisteredWorkflowFile(path_workflow_id(COPY), (Path("/elsewhere.py"),), file_present=True),
        ]

        workflow_id, reason = _decide_one(FoundWorkflowFile(COPY, header_id=HEADER_ID), *registered)

        assert reason is WorkflowIdReason.DUPLICATE
        assert workflow_id not in {HEADER_ID, path_workflow_id(COPY)}
        assert is_workflow_id(workflow_id)


class TestAFileMovedOutsideTheEditor:
    def test_takes_over_the_entry_whose_file_is_gone(self) -> None:
        gone = RegisteredWorkflowFile(HEADER_ID, (ORIGINAL,), file_present=False)

        (decision,) = reconcile_workflow_ids([FoundWorkflowFile(COPY, header_id=HEADER_ID)], [gone])

        assert decision.workflow_id == HEADER_ID
        assert decision.reason is WorkflowIdReason.MOVED
        assert decision.entry_id == HEADER_ID

    def test_only_one_of_two_files_carrying_the_id_takes_it_over(self) -> None:
        gone = RegisteredWorkflowFile(HEADER_ID, (Path("/workspace/gone.py"),), file_present=False)
        found = [FoundWorkflowFile(COPY, header_id=HEADER_ID), FoundWorkflowFile(ORIGINAL, header_id=HEADER_ID)]

        reasons = {decision.file.path: decision.reason for decision in reconcile_workflow_ids(found, [gone])}

        assert reasons == {COPY: WorkflowIdReason.MOVED, ORIGINAL: WorkflowIdReason.DUPLICATE}


class TestAPeersId:
    def test_wins_over_the_entry_registered_at_the_path(self) -> None:
        registered = RegisteredWorkflowFile(OTHER_ID, (ORIGINAL,), file_present=True)

        (decision,) = reconcile_workflow_ids([FoundWorkflowFile(ORIGINAL, peer_id=PEER_ID)], [registered])

        assert decision.workflow_id == PEER_ID
        assert decision.reason is WorkflowIdReason.PEER
        assert decision.displaced_id == OTHER_ID
        assert decision.entry_id is None

    def test_moves_its_entry_from_another_path(self) -> None:
        registered = RegisteredWorkflowFile(PEER_ID, (ORIGINAL,), file_present=True)

        (decision,) = reconcile_workflow_ids([FoundWorkflowFile(COPY, peer_id=PEER_ID)], [registered])

        assert decision.entry_id == PEER_ID
        assert decision.displaced_id is None
