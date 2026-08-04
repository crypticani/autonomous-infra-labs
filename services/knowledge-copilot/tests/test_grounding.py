from app import build_context, extract_markers, ground_answer, strip_markers
from retrieval import Hit


def hit(source="oomkilled-pod.md", index=0, text="body", score=0.7) -> Hit:
    return Hit(
        text=text, source=source, chunk_index=index, doc_type="runbook", score=score
    )


def test_context_block_is_byte_exact():
    context = build_context(
        [hit(text="first"), hit(source="tls-cert-expiry.md", index=2, text="second")]
    )
    assert context == (
        "<context>\n"
        '<chunk id="1" source="oomkilled-pod.md" chunk_index="0">\n'
        "first\n"
        "</chunk>\n"
        '<chunk id="2" source="tls-cert-expiry.md" chunk_index="2">\n'
        "second\n"
        "</chunk>\n"
        "</context>"
    )


def test_markers_are_deduplicated_in_order_of_use():
    assert extract_markers("a [2] b [1] c [2] d") == [2, 1]
    assert extract_markers("no citations here") == []


def test_shell_subscripts_are_not_markers():
    assert extract_markers("check ${nodes[0]} and argv[1], then see [2]") == [2]
    assert extract_markers("kubectl get po -o jsonpath='{.items[0].spec}'") == []


def test_stripping_leaves_punctuation_clean():
    assert strip_markers("keep [1], drop [7].", {7}) == "keep [1], drop."
    assert strip_markers("untouched [1].", set()) == "untouched [1]."
    assert strip_markers("keep ${n[0]} drop [9].", {9}) == "keep ${n[0]} drop."


def test_no_chunks_is_never_grounded():
    answer, sources, grounded = ground_answer("anything [1]", [])
    assert grounded is False
    assert sources == []
    assert "[1]" not in answer


def test_chunks_without_citations_are_not_grounded():
    answer, sources, grounded = ground_answer("plain prose, no markers", [hit()])
    assert grounded is False
    assert sources == []
    assert answer == "plain prose, no markers"


def test_one_invented_marker_strips_and_ungrounds():
    hits = [hit(), hit(source="tls-cert-expiry.md", index=1, score=0.71)]
    answer, sources, grounded = ground_answer("good [2] bad [9]", hits)
    assert grounded is False
    assert [s.marker for s in sources] == [2]
    assert [s.source for s in sources] == ["tls-cert-expiry.md"]
    assert "[9]" not in answer


def test_all_markers_resolving_is_grounded():
    hits = [hit(), hit(source="tls-cert-expiry.md", index=1, score=0.712)]
    answer, sources, grounded = ground_answer("both [1] and [2]", hits)
    assert grounded is True
    assert [s.marker for s in sources] == [1, 2]
    assert sources[1].score == 0.712
    assert answer == "both [1] and [2]"
