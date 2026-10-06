"""Near-duplicate claim guard (2026-10-06): paraphrases merge; updates with
changed numbers/dates and different files never do."""

from server.services.memory.claim_service import near_duplicate


def test_paraphrase_is_duplicate():
    a = ("Suggested shared activity for the Bordeaux/Cadillac area; manageable "
         "drive from Cadillac; suitable for kids; structured outing")
    assert near_duplicate(a, a + " in July heat")


def test_changed_number_is_an_update():
    a = "Quote for the Rupert mug order is $45 including postage to his Perth address"
    b = "Quote for the Rupert mug order is $50 including postage to his Perth address"
    assert not near_duplicate(a, b)


def test_paths_are_never_prose_duplicates():
    assert not near_duplicate("reviews/literary/2026-06-14-review.md",
                              "reviews/literary/2026-06-14-review.docx")


def test_different_facts_stay_separate():
    a = "2026-09-02: Bob accepted Sean's bet — $50 on Bob making some kind of profit in week 1"
    b = "2026-09-02: Bet direction clarified — Sean's $50 is FOR Bob making a profit in week 1"
    assert not near_duplicate(a, b)
