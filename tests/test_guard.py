from debeloglav import guard

# Varied, non-repeating Slovenian prose about a cycling race. Long enough that
# trim_runaway examines it, but natural text compresses too poorly per window
# to look like a loop, so the guard must keep it unchanged.
PROSE = (
    "Včerajšnja gorska etapa dirke Po Sloveniji je pokazala, kdo je trenutno v najboljši formi. "
    "Na zadnjem vzponu so favoriti sicer držali korak, a je nato iz skupine napadel mladi kolesar iz Novega mesta. "
    "Trener je po cilju razložil, da je ekipa taktiko izvajala natanko po načrtu, ki so ga pripravili že zgodaj zjutraj. "
    "Sončno vreme je kolesarjem polepšalo dan, medtem ko je močan veter na dolžem spustu marsikoga presenetil. "
    "Po prihodu v cilj so tekmovalci najprej poskrbeli za tekočino, nato pa so se zahvalili navijačem ob progi. "
    "Če bo vožnja v naslednjih dneh podobna, lahko v nedeljo pričakujemo izjemno zanimiv zaključek etape. "
    "Direktor dirke je pohvalil organizacijsko ekipo in obljubil, da bodo prihodnje leto pripravili še več zanimivih prehodov. "
    "Na koncu je zmagal tisti, ki je najbolje obvladal zadnjih pet kilometrov in ohranil mirno glavo do črte cilja."
)


def test_normal_text_is_kept():
    assert guard.trim_runaway(PROSE) == PROSE


def test_short_text_is_kept():
    assert guard.trim_runaway("ja ja ja ja") == "ja ja ja ja"


def test_loop_is_cut_at_a_sentence_end():
    text = PROSE + "adijo adijo " * 200
    trimmed = guard.trim_runaway(text)
    assert trimmed.endswith(" …")
    assert "adijo adijo adijo" not in trimmed
    assert trimmed.startswith(PROSE[:300])


def test_cap_keeps_short_text():
    assert guard.cap("kratko", 100) == "kratko"


def test_cap_cuts_at_a_paragraph():
    text = "a" * 60 + "\n\n" + "b" * 60
    assert guard.cap(text, 100) == "a" * 60 + "\n…"


def test_cap_cuts_at_a_sentence():
    text = "Prvi stavek je tu. " * 10
    capped = guard.cap(text, 100)
    assert len(capped) <= 102
    assert capped.endswith("tu.\n…")


def test_cap_falls_back_to_a_word():
    text = "beseda " * 40
    capped = guard.cap(text, 100)
    assert capped.endswith(" …")
    assert len(capped) <= 102
