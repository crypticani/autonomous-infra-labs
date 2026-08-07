from sessions import (
    HISTORY_TURNS_IN_PROMPT,
    HISTORY_TURNS_IN_QUERY,
    SESSION_MAX_TURNS,
    SESSION_TTL,
    Turn,
    append,
    history,
    prompt_history,
    retrieval_query,
)

T1 = Turn("why is appsrv disk filling up?", "The root filesystem is 79% full [1].")
T2 = Turn("what's the command to clear it?", "Run journalctl --vacuum-time=2d [1].")


def test_a_new_thread_has_no_history():
    assert history("1755000000.000100", now=1000.0) == []


def test_append_then_read_back():
    append("t1", T1, now=1000.0)
    assert history("t1", now=1000.0) == [T1]


def test_threads_do_not_share_history():
    append("t1", T1, now=1000.0)
    append("t2", T2, now=1000.0)
    assert history("t1", now=1000.0) == [T1]


def test_history_is_capped_and_drops_the_oldest_turn():
    total = SESSION_MAX_TURNS + 3
    for i in range(total):
        append("t1", Turn(f"q{i}", f"a{i}"), now=1000.0)

    turns = history("t1", now=1000.0)
    assert len(turns) == SESSION_MAX_TURNS
    assert turns[0].question == f"q{total - SESSION_MAX_TURNS}"
    assert turns[-1].question == f"q{total - 1}"


def test_an_idle_thread_is_evicted_after_the_ttl():
    append("t1", T1, now=1000.0)
    assert history("t1", now=1000.0 + SESSION_TTL + 1) == []


def test_activity_keeps_a_thread_alive():
    append("t1", T1, now=1000.0)
    append("t1", T2, now=1000.0 + SESSION_TTL - 1)
    # The second append refreshed last_used, so the first turn survives past the point
    # it would have expired on its own.
    assert len(history("t1", now=1000.0 + SESSION_TTL + 1)) == 2


def test_retrieval_query_is_the_question_alone_without_history():
    assert retrieval_query([], T1.question) == T1.question


def test_a_followup_is_prefixed_with_the_previous_question():
    # "what's the command to clear it?" scores below the 0.65 floor on its own and
    # comes back "Not covered". Prefixed, it retrieves what answered turn one.
    assert retrieval_query([T1], T2.question) == f"{T1.question} {T2.question}"


def test_the_query_carries_less_history_than_the_prompt():
    # Every extra word shifts BM25's term weighting and drags the dense vector toward
    # the corpus centroid, so the query deliberately sees less than the prompt.
    assert HISTORY_TURNS_IN_QUERY < HISTORY_TURNS_IN_PROMPT

    turns = [Turn(f"q{i}", f"a{i}") for i in range(4)]
    assert retrieval_query(turns, "new") == "q3 new"


def test_prompt_history_is_empty_without_turns():
    assert prompt_history([]) == ""


def test_prompt_history_carries_the_last_two_turns():
    turns = [Turn(f"q{i}", f"a{i}") for i in range(4)]
    block = prompt_history(turns)

    assert block.startswith("<history>")
    assert "q2" in block and "q3" in block
    assert "q1" not in block
