from eval_retrieval import sweep_counts


def test_sweep_counts_at_a_low_floor():
    """Low floor: nothing answerable is rejected, but absent questions sneak through."""
    answerable = [0.72, 0.66, 0.61]
    absent = [0.61, 0.63]
    false_rejects, false_accepts = sweep_counts(answerable, absent, floor=0.60)
    assert false_rejects == 0
    assert false_accepts == 2


def test_sweep_counts_at_a_high_floor():
    answerable = [0.72, 0.66, 0.61]
    absent = [0.61, 0.63]
    false_rejects, false_accepts = sweep_counts(answerable, absent, floor=0.70)
    assert false_rejects == 2
    assert false_accepts == 0


def test_sweep_counts_boundary_is_inclusive():
    """retrieve keeps a hit when `score >= floor`. The sweep must agree exactly, or the
    floor it recommends will be off by one bucket at the boundary."""
    assert sweep_counts([0.65], [], floor=0.65) == (0, 0)
    assert sweep_counts([], [0.65], floor=0.65) == (0, 1)
