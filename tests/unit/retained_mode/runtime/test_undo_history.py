from griptape_nodes.retained_mode.runtime.undo_history import MAX_STEPS, UndoHistory


def _with_steps(*labels: str) -> UndoHistory:
    """A history at the end of a step for each label, its checkpoints holding the label as their commands."""
    history = UndoHistory()
    history.record(b"start")
    for label in labels:
        history.pending_label = label
        history.record(label.encode())
    return history


class TestAnEmptyHistory:
    def test_has_nothing_to_undo_or_redo(self) -> None:
        history = UndoHistory()

        assert history.undo_label is None
        assert history.redo_label is None
        assert history.needs_checkpoint

    def test_its_first_checkpoint_is_not_a_step(self) -> None:
        history = UndoHistory()

        history.record(b"start")

        assert history.undo_label is None
        assert not history.needs_checkpoint


class TestSteps:
    def test_a_pending_step_can_be_undone_before_it_is_checkpointed(self) -> None:
        history = _with_steps()

        history.pending_label = "Move"

        assert history.undo_label == "Move"
        assert history.needs_checkpoint
        assert history.previous() is None, "the current checkpoint is out of date until one is taken"

    def test_checkpointing_a_step_adds_one(self) -> None:
        history = _with_steps("Move")

        previous = history.previous()

        assert previous is not None
        assert previous.commands == b"start"
        assert history.undo_label == "Move"

    def test_stepping_back_and_forth_moves_the_labels(self) -> None:
        history = _with_steps("Move", "Rename")

        history.step_back()

        assert history.undo_label == "Move"
        assert history.redo_label == "Rename"
        next_checkpoint = history.next()
        assert next_checkpoint is not None
        assert next_checkpoint.commands == b"Rename"

        history.step_forward()

        assert history.undo_label == "Rename"
        assert history.redo_label is None

    def test_a_new_step_drops_what_could_be_redone(self) -> None:
        history = _with_steps("Move", "Rename")
        history.step_back()

        history.pending_label = "Delete"
        history.record(b"Delete")

        assert history.redo_label is None
        assert history.undo_label == "Delete"
        previous = history.previous()
        assert previous is not None
        assert previous.commands == b"Move"

    def test_only_the_latest_steps_are_kept(self) -> None:
        history = _with_steps(*(f"step {number}" for number in range(MAX_STEPS + 5)))

        undone = 0
        while history.previous() is not None:
            history.step_back()
            undone += 1

        assert undone == MAX_STEPS


class TestChangesOutsideAStep:
    def test_replace_the_current_checkpoint_instead_of_adding_a_step(self) -> None:
        history = _with_steps("Move")

        history.stale = True
        history.record(b"Move, then a run")

        assert history.undo_label == "Move"
        current = history.current()
        assert current is not None
        assert current.commands == b"Move, then a run"
        assert current.label == "Move"

    def test_can_drop_what_could_be_redone(self) -> None:
        history = _with_steps("Move", "Rename")
        history.step_back()

        history.drop_redo()

        assert history.undo_label == "Move"
        assert history.redo_label is None

    def test_keep_what_could_be_redone(self) -> None:
        history = _with_steps("Move", "Rename")
        history.step_back()

        history.stale = True
        history.record(b"Move, then a run")

        assert history.redo_label == "Rename"


class TestClearing:
    def test_forgets_every_checkpoint(self) -> None:
        history = _with_steps("Move")
        history.open_group = "drag"

        history.clear()

        assert history.undo_label is None
        assert history.current() is None
        assert history.open_group is None
        assert history.needs_checkpoint
