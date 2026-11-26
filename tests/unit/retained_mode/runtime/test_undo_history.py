from griptape_nodes.retained_mode.runtime.undo_history import (
    MAX_STEPS,
    Checkpointed,
    Clean,
    OpenStep,
    OutsideEdit,
    RunOutput,
    StepPending,
    UndoHistory,
)


def _record(history: UndoHistory, commands: bytes) -> Checkpointed:
    """Take a checkpoint of ``commands``, as differing from a checkpoint whose commands aren't the same."""
    return history.record(commands, differs=lambda checkpoint: checkpoint != commands)


def _step(history: UndoHistory, label: str) -> None:
    """Note a change by a step called ``label``."""
    step = OpenStep(group=label, label=label)
    history.open_step = step
    history.note(StepPending(step))


def _step_back(history: UndoHistory) -> None:
    """Undo, as a rebuild that left the workflow exactly as the previous checkpoint holds it."""
    previous = history.previous()
    assert previous is not None
    history.step_back(previous.commands)


def _step_forward(history: UndoHistory) -> None:
    following = history.next()
    assert following is not None
    history.step_forward(following.commands)


def _with_steps(*labels: str) -> UndoHistory:
    """A history at the end of a step for each label, its checkpoints holding the label as their commands."""
    history = UndoHistory()
    _record(history, b"start")
    for label in labels:
        _step(history, label)
        _record(history, label.encode())
    return history


class TestAnEmptyHistory:
    def test_has_nothing_to_undo_or_redo(self) -> None:
        history = UndoHistory()

        assert history.undo_label is None
        assert history.redo_label is None
        assert history.needs_checkpoint

    def test_its_first_checkpoint_is_not_a_step(self) -> None:
        history = UndoHistory()

        checkpointed = _record(history, b"start")

        assert checkpointed is Checkpointed.STARTED
        assert history.undo_label is None
        assert not history.needs_checkpoint


class TestSteps:
    def test_a_pending_step_can_be_undone_before_it_is_checkpointed(self) -> None:
        history = _with_steps()

        _step(history, "Move")

        assert history.undo_label == "Move"
        assert history.needs_checkpoint
        assert history.previous() is None, "the current checkpoint is out of date until one is taken"

    def test_checkpointing_a_step_adds_one(self) -> None:
        history = _with_steps("Move")

        previous = history.previous()

        assert previous is not None
        assert previous.commands == b"start"
        assert history.undo_label == "Move"

    def test_a_step_that_left_the_workflow_as_it_was_adds_none(self) -> None:
        history = _with_steps("Move")

        _step(history, "Set Value")
        checkpointed = _record(history, b"Move")

        assert checkpointed is Checkpointed.UNCHANGED
        assert history.undo_label == "Move"
        assert not history.needs_checkpoint

    def test_a_step_that_differs_only_in_what_an_undo_would_keep_adds_none(self) -> None:
        """Such as a seed a run rolled. The current checkpoint takes it in."""
        history = _with_steps("Move")

        _step(history, "Set Value")
        checkpointed = history.record(b"Move, then a run", differs=lambda _checkpoint: False)

        assert checkpointed is Checkpointed.UNCHANGED
        assert history.undo_label == "Move"
        current = history.current()
        assert current is not None
        assert current.commands == b"Move, then a run"

    def test_a_step_takes_the_label_its_requests_name_it_by_last(self) -> None:
        history = _with_steps()
        _step(history, "Set Parameter Value")
        assert history.open_step is not None

        history.open_step.label = "Group 3 Nodes"
        _record(history, b"grouped")

        current = history.current()
        assert current is not None
        assert current.label == "Group 3 Nodes"

    def test_stepping_back_and_forth_moves_the_labels(self) -> None:
        history = _with_steps("Move", "Rename")

        _step_back(history)

        assert history.undo_label == "Move"
        assert history.redo_label == "Rename"
        next_checkpoint = history.next()
        assert next_checkpoint is not None
        assert next_checkpoint.commands == b"Rename"

        _step_forward(history)

        assert history.undo_label == "Rename"
        assert history.redo_label is None

    def test_stepping_back_closes_the_open_step(self) -> None:
        history = _with_steps("Move")

        _step_back(history)

        assert history.open_step is None

    def test_a_new_step_drops_what_could_be_redone(self) -> None:
        history = _with_steps("Move", "Rename")
        _step_back(history)

        _step(history, "Delete")
        checkpointed = _record(history, b"Delete")

        assert checkpointed is Checkpointed.ADDED
        assert history.redo_label is None
        assert history.undo_label == "Delete"
        previous = history.previous()
        assert previous is not None
        assert previous.commands == b"Move"

    def test_only_the_latest_steps_are_kept(self) -> None:
        history = _with_steps(*(f"step {number}" for number in range(MAX_STEPS + 5)))

        undone = 0
        while history.previous() is not None:
            _step_back(history)
            undone += 1

        assert undone == MAX_STEPS


class TestARun:
    def test_replaces_the_current_checkpoint_instead_of_adding_a_step(self) -> None:
        history = _with_steps("Move")

        history.note(RunOutput())
        checkpointed = _record(history, b"Move, then a run")

        assert checkpointed is Checkpointed.REPLACED
        assert history.undo_label == "Move"
        current = history.current()
        assert current is not None
        assert current.commands == b"Move, then a run"
        assert current.label == "Move"

    def test_keeps_what_could_be_redone(self) -> None:
        history = _with_steps("Move", "Rename")
        _step_back(history)

        history.note(RunOutput())
        _record(history, b"Move, then a run")

        assert history.redo_label == "Rename"


class TestAClientsChangeOutsideAStep:
    def test_replaces_the_current_checkpoint_instead_of_adding_a_step(self) -> None:
        history = _with_steps("Move")

        history.note(OutsideEdit())
        _record(history, b"Move, then a resize")

        assert history.undo_label == "Move"
        current = history.current()
        assert current is not None
        assert current.commands == b"Move, then a resize"

    def test_drops_what_could_be_redone(self) -> None:
        history = _with_steps("Move", "Rename")
        _step_back(history)

        history.note(OutsideEdit())
        _record(history, b"Move, then a resize")

        assert history.undo_label == "Move"
        assert history.redo_label is None

    def test_that_left_the_workflow_as_it_was_keeps_what_could_be_redone(self) -> None:
        """Such as an editor writing back what it shows after an undo."""
        history = _with_steps("Move", "Rename")
        _step_back(history)

        history.note(OutsideEdit())
        _record(history, b"Move")

        assert history.redo_label == "Rename"

    def test_that_differs_only_in_what_an_undo_would_keep_keeps_what_could_be_redone(self) -> None:
        history = _with_steps("Move", "Rename")
        _step_back(history)

        history.note(OutsideEdit())
        history.record(b"Move, then a run", differs=lambda _checkpoint: False)

        assert history.redo_label == "Rename"


class TestChangesBeforeOneCheckpoint:
    def test_a_run_does_not_hide_a_clients_change(self) -> None:
        history = _with_steps("Move", "Rename")
        _step_back(history)

        history.note(OutsideEdit())
        history.note(RunOutput())
        _record(history, b"Move, then a resize and a run")

        assert history.redo_label is None

    def test_a_clients_change_outside_a_step_does_not_hide_a_step(self) -> None:
        history = _with_steps("Move")

        _step(history, "Rename")
        history.note(OutsideEdit())
        _record(history, b"Rename, then a resize")

        assert history.undo_label == "Rename"
        previous = history.previous()
        assert previous is not None
        assert previous.commands == b"Move"

    def test_a_checkpoint_leaves_the_history_clean(self) -> None:
        history = _with_steps("Move")

        history.note(OutsideEdit())
        _record(history, b"Move, then a resize")

        assert history.drift == Clean()
        assert not history.needs_checkpoint


class TestOpenStep:
    def test_is_running_while_any_of_its_requests_is(self) -> None:
        step = OpenStep(group="drag", label="Move")

        step.request_started()
        step.request_started()
        step.request_finished()

        assert step.running
        step.request_finished()
        assert not step.running


class TestClearing:
    def test_forgets_every_checkpoint(self) -> None:
        history = _with_steps("Move")
        history.open_step = OpenStep(group="drag", label="Move")

        history.clear()

        assert history.undo_label is None
        assert history.current() is None
        assert history.open_step is None
        assert history.needs_checkpoint


class TestARebuild:
    def test_becomes_the_checkpoint_it_was_rebuilt_from(self) -> None:
        """Rebuilding can reorder the nodes, which a change that leaves the workflow as it was must not see."""
        history = _with_steps("Move", "Rename")

        history.step_back(b"Move, reordered")
        history.note(OutsideEdit())
        _record(history, b"Move, reordered")

        current = history.current()
        assert current is not None
        assert current.commands == b"Move, reordered"
        assert current.label == "Move"
        assert history.redo_label == "Rename"
