"""Run-together text in bot-to-bot messages and agent briefs is refused."""

import pytest

from django_ergo.bots.plain_text import check_spacing
from django_ergo.bots.plain_text import run_together_words

# Real messages one Storytime chat sent another (2026-11-10).
GARBLED = [
    "Measured openingboundaryconfirmed: deliveredVO003guestline ends14.352172s, "
    "existingadded.092sgap ends14.444172s beforeVO004. Insertnewcontext at14.444172s "
    "preservingallprior audio/picture/subtitles unchanged. First10slock satisfied.",
    "Productionrequests exactv3source staging beforeapprovedimageexecution "
    "(currentlyawaitingcombinedspendapproval,no paidcalls). Please use your "
    "ownlocal_shell_upload to stage listeningmaster andadaptation intoScript-owned "
    "localinputjob and authorizeexactsharedhostREADCOPYpaths here.",
]

CLEAN = [
    # Long words, paths, file names, ids, hashes and hyphenated compounds are fine.
    "Comprehensibility matters: the internationalization review found "
    "characterization problems in the counterintuitive opening.",
    "Staged /home/dev/agent-jobs/storytime/pilot-a-ch01-v3-approved-inputs-20261110-v1/"
    "inputs/pilot-A-ch01-listening-master-v3-draft.md (SHA256 "
    "14ba6b84a1d6c481bf1b50a35deab73dbc8732775cc4f09661f7118c5dfe0b17).",
    "Run gemini-3.1-flash-image with --allow-unverified-price; thoughtsTokenCount "
    "is billed. Voice N2lVS1w4EtoT3dr4eOWO, files pilot-b-visual-cost-approval-v1.md "
    "and kit-fix-request-3.1.md, all-output-at-image-rate.",
    "Lee approved Chapter 2 pitch option 2, The Bride's Choice, at 17:38 UTC.",
]


@pytest.mark.parametrize("text", GARBLED)
def test_run_together_text_is_refused(text):
    with pytest.raises(ValueError, match="words run together") as refused:
        check_spacing(text, "message")
    assert "plain sentences" in str(refused.value)


@pytest.mark.parametrize("text", CLEAN)
def test_normal_text_passes(text):
    check_spacing(text, "message")


def test_a_stray_long_word_in_a_long_message_passes():
    text = ("The chapter keeps moving and every scene changes something. " * 20) + (
        "Wordsruntogetherhere once, alsosomewhereelsetoo, andathirdtimeoverhere."
    )
    assert len(run_together_words(text)) == 3
    check_spacing(text, "message")  # 3 in ~200 words is under the share
