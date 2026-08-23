"""What a grounded answer costs, and what k costs -- Day 27.

Four calls, one per k, rather than four calls at the same k. A mean of four identical
questions would be one number with an error bar; a sweep across k is a scaling curve,
and for this service the curve is the answer. Prompt eval dominates on CPU -- 195s was
measured in week 2 for one grounded answer over four ~512-char chunks -- and prompt
length is set almost entirely by how many chunks got retrieved. So cost here is a
function of k, not of the question, and `k` is a per-request parameter that any caller
can raise.

Same lesson as security-triage's batch-size sweep, from the other direction: there the
fixed system prompt is amortised across more findings as the batch grows, so bigger is
cheaper per unit. Here every extra chunk is pure additional prompt with nothing amortised
against it, so bigger is straightforwardly more expensive, and the question is what the
retrieval quality is worth.

    python bench.py                        # k = 1,2,4,8 against one question
    python bench.py --ks 4 --question "..."

Reads ST-style totals off the provider by delta; see llm.py's BaseLLMProvider.
"""

import argparse
import time

from app import answer_question
from llm import get_llm_provider

DEFAULT_QUESTION = "how do I roll back a bad deploy?"


def run_k(question: str, k: int) -> dict:
    provider = get_llm_provider()
    prompt_before, output_before = provider.prompt_tokens, provider.output_tokens

    started = time.monotonic()
    response = answer_question(question, k=k)
    elapsed = time.monotonic() - started

    return {
        "k": k,
        "elapsed": elapsed,
        "prompt_tokens": provider.prompt_tokens - prompt_before,
        "output_tokens": provider.output_tokens - output_before,
        # Whether the answer was actually usable at this k, not just how fast it came
        # back. A refusal is the cheapest possible answer and the least useful one, so a
        # cost table without this column would rank k=1 best for the wrong reason.
        "grounded": response.grounded,
        "sources": len(response.sources),
        "answer_chars": len(response.answer),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--ks", default="1,2,4,8", help="comma-separated k values")
    args = parser.parse_args()

    provider = get_llm_provider()
    print(f"provider: {provider.name} ({provider.model_name})")
    print(f"question: {args.question}")
    print()
    print(
        f"{'k':>3} {'sec':>8} {'p_tok':>8} {'o_tok':>8} {'p_tok/k':>8} "
        f"{'grounded':>9} {'srcs':>5} {'ans_ch':>7}"
    )

    for k in [int(value) for value in args.ks.split(",")]:
        row = run_k(args.question, k)
        # Printed per row: on CPU each of these is minutes, and a run interrupted at k=8
        # should still leave the earlier points on screen.
        print(
            f"{row['k']:>3} {row['elapsed']:>8.1f} {row['prompt_tokens']:>8} "
            f"{row['output_tokens']:>8} {row['prompt_tokens'] / row['k']:>8.1f} "
            f"{str(row['grounded']):>9} {row['sources']:>5} {row['answer_chars']:>7}"
        )


if __name__ == "__main__":
    main()
