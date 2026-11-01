"""Understands every kind of Bengali the user writes:
- standard Bangla (শুদ্ধ / চলিত), Bangla digits (০-৯)
- Banglish: Bangla written in English letters ("ami kal dhaka jabo, ticket ache?")
- regional dialects: Sylheti, Chittagonian, Noakhali, Barishal, Rangpur, Rajshahi, Dhaka colloquial
- mixed Bangla + English in one message

Before the assistant answers, a quick step works out what the message MEANS (standard Bangla + English) and which
form the user wrote in. The meaning is added to the message so the brain and the ability picker understand it;
the reply comes back in the user's own style. Dates like আজ / কাল / পরশু are turned into real dates.
"""
import datetime
import json
import logging
import re

from config import CFG

log = logging.getLogger("kakarot.bangla")

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# common Bangla words written in English letters (several spellings each)
BANGLISH = set("""
ami amra amar amader amake tumi tomra tomar tomader tomake apni apnara apnar apnader apnake tui tor toke se shey tar
tara tader take oi oita ei eita eta ota sheta kon konta kono kichu kisu keu kew kar kake kahake shob sob sobai shobai
ki kি kno keno kanw kivabe kibhabe kemne kemon kothay kotha koi kobe koto koyta koita kar kotokkhon
acho achen aso asen achi asi ache ase achhe nai nei na naki to tou ar aro kintu kinto ebong ba othoba jodi tahole tobe
bhalo valo bhalo kharap sundor shundor thik thikache accha achha acha aacha hmm haan han ji jii
korbo korte koro koren korsi korechi korlam korlen korun koris korish kortesi kortechi korchi korcho korben korte
dao den dibo dite diyo dio diben nibo nite nao nen niyo neo nilam
jabo jaibo jamu jao jan jete jaite jaitesi jacchi jachi gelam gesi gechi gese geche
asbo ashbo aste ashte aso esho eso asho asen ashen aisi esechi eseche
chai chaai lagbe lage lagse lagche lagbo bolo bolen bolte bolsi bolechi bollam bolben bolo
dekho dekhen dekhte dekhao dekhan dekhsi dekhechi khabo khaite khete khai khaisi kheyechi khao khan
kal kalke kalkে aj ajke aaj aajke porshu porsu ekhon akhon ekhuni pore age din raat rat sokal shokal bikal
bikel shondha sondha dupur shoptaho mash bochor
taka poisa daam dam bhara vara bhai vai vaia bhaiya apu apa mama khala chacha dost bondhu
ekta akta ekti duita duto tinta tinte charta onek kom beshi besi bes shudhu sudhu ektu
jani jana janina janen jano bujhi bujhlam bujhte bujhina bujhi paro paren parbo parba pari parben parsi
dorkar kaj kam bari basa basay office shomoy somoy shomossa somossa problem kotobar
jonno theke diye moto mone chinta bhabo bhabi khobor khabar pani gari jaiga jayga
hobe hoy hoise hoyeche hoyche holo hoilo hocche hosse hoyni hoini hoite hote
""".split())

# words that are also normal English - never count them alone
EN_COMMON = {"to", "ar", "ha", "na", "ki", "se", "dam", "bus", "ticket", "office", "problem", "mama", "din", "kon",
             "tar", "take", "eta", "oi", "ba", "pari", "dao", "jan", "pore", "age", "kal", "aj", "rat", "mash", "hoy",
             "jodi", "tobe", "ami", "acha", "dost", "aso", "asen"}

# dialect words (Bangla script and English letters) -> standard Bangla, used as hints for the brain
DIALECTS = {
    "Sylheti": {"কিতা": "কী", "কেনে": "কেন", "ইতা": "এটা", "অইছে": "হয়েছে", "যাইতাম": "যাব", "ফুরি": "মেয়ে",
                "ফুয়া": "ছেলে", "মাত": "কথা", "আমরার": "আমাদের", "তুমরার": "তোমাদের", "কুনো": "কোনো", "অখন": "এখন",
                "kita": "কী", "kene": "কেন", "ita": "এটা", "oise": "হয়েছে", "zaitam": "যাব", "jaitam": "যাব",
                "furi": "মেয়ে", "fua": "ছেলে", "maat": "কথা", "amrar": "আমাদের", "tumrar": "তোমাদের", "okhon": "এখন"},
    "Chittagonian": {"আঁই": "আমি", "তুঁই": "তুমি", "গরি": "করে", "গরো": "করো", "কিল্লাই": "কেন", "হডে": "কোথায়",
                     "এডে": "এখানে", "যাইয়ুম": "যাব", "খাইয়ুম": "খাব", "ফোয়া": "ছেলে", "গইজ্জে": "করেছে",
                     "aai": "আমি", "gori": "করে", "goro": "করো", "killai": "কেন", "hode": "কোথায়", "ede": "এখানে",
                     "jaiyum": "যাব", "khaiyum": "খাব", "goijje": "করেছে"},
    "Noakhali": {"হিয়ানে": "সেখানে", "হেতে": "সে (ছেলে)", "হেতি": "সে (মেয়ে)", "আঙ্গো": "আমাদের", "কিত্তাম": "কী করব",
                 "কিয়া": "কী", "hiyane": "সেখানে", "hete": "সে (ছেলে)", "heti": "সে (মেয়ে)", "ango": "আমাদের",
                 "kittam": "কী করব"},
    "Barishal": {"মুই": "আমি", "মোর": "আমার", "মোগো": "আমাদের", "তোমাগো": "তোমাদের", "হ্যারা": "তারা",
                 "কাইল্লা": "কাল", "mui": "আমি", "mor": "আমার", "mogo": "আমাদের", "tomago": "তোমাদের",
                 "hera": "তারা", "kailla": "কাল"},
    "Rangpur": {"বাহে": "ভাই (ডাকার শব্দ)", "কোনটে": "কোথায়", "হামরা": "আমরা", "হামার": "আমার", "যাইম": "যাব",
                "খাইম": "খাব", "bahe": "ভাই", "konte": "কোথায়", "hamra": "আমরা", "hamar": "আমার", "jaim": "যাব",
                "khaim": "খাব"},
    "Rajshahi": {"হামি": "আমি", "hami": "আমি"},
    "Dhaka colloquial": {"যামু": "যাব", "খামু": "খাব", "করমু": "করব", "দিমু": "দেব", "কইলাম": "বললাম", "কইছি": "বলেছি",
                         "কইছে": "বলেছে", "জামু": "যাব", "jamu": "যাব", "khamu": "খাব", "kormu": "করব",
                         "dimu": "দেব", "koilam": "বললাম", "koisi": "বলেছি", "koise": "বলেছে"},
}

DATE_WORDS = [  # (pattern, day offset, label)
    (r"আজকে|আজ|\baj(ke)?\b|\baaj(ke)?\b", 0, "today"),
    (r"আগামীকাল|আগামী কাল|কালকে|কাইলকা|কাইলকে|কাইল্লা|কাল|\bkal(ke)?\b|\bagamikal\b|\bkailla\b", 1, "tomorrow (or yesterday if the "
                                                                                   "sentence is about the past)"),
    (r"পরশু|\bporshu\b|\bporsu\b", 2, "day after tomorrow"),
]

UNDERSTAND_PROMPT = """The user wrote this message to their assistant (it may be Bangla, Banglish = Bangla in English
letters, a regional Bangla dialect, or a mix of Bangla and English):
\"\"\"{text}\"\"\"
{hints}
Work out exactly what the user means. Keep names, places, numbers, prices, dates, links and English technical
words exactly as written. Reply ONLY with JSON:
{{"form": "one of: standard bangla, banglish, dialect, mixed, english",
  "dialect": "Sylheti / Chittagonian / Noakhali / Barishal / Rangpur / Rajshahi / Dhaka colloquial / none",
  "standard_bangla": "the message in clear standard Bangla",
  "english": "the message's full meaning in clear English",
  "unclear": "any word you are not sure about, else empty"}}"""


def has_bangla_script(text):
    return len(re.findall(r"[ঀ-৿]", text or "")) >= 2


def banglish_hits(text):
    words = re.findall(r"[a-z]+", (text or "").lower())
    hits = [w for w in words if w in BANGLISH]
    strong = [w for w in hits if w not in EN_COMMON]
    return words, hits, strong


def is_banglish(text):
    words, hits, strong = banglish_hits(text)
    if not words or has_bangla_script(text):
        return False
    ratio = len(hits) / len(words)
    return (len(set(strong)) >= 1 and len(set(hits)) >= 2 and ratio >= 0.2) or len(set(strong)) >= 3


def dialect_hints(text):
    """[(dialect, word, standard)] found in the text."""
    low = (text or "").lower()
    found = []
    for name, words in DIALECTS.items():
        for w, std in words.items():
            if re.search(r"[ঀ-৿]", w):
                if re.search(r"(?<![ঀ-৿])" + re.escape(w) + r"(?![ঀ-৿])", text or ""):
                    found.append((name, w, std))
            elif re.search(r"\b" + re.escape(w) + r"\b", low):
                found.append((name, w, std))
    return found


def guess_dialect(text):
    hits = dialect_hints(text)
    if not hits:
        return None
    count = {}
    for name, _, _ in hits:
        count[name] = count.get(name, 0) + 1
    best = max(count, key=count.get)
    return best if count[best] >= (1 if any(re.search(r"[ঀ-৿]", w) for _, w, _ in hits) else 2) else None


def date_hints(text, today=None):
    today = today or datetime.date.today()
    out = []
    for pat, off, label in DATE_WORDS:
        if re.search(pat, text or "", re.I):
            out.append(f"{label} = {(today + datetime.timedelta(days=off)).isoformat()}")
    return out


def needs_help(text):
    return bool(text and (has_bangla_script(text) or is_banglish(text) or guess_dialect(text)))


def understand(text):
    """Ask the brain what the message means. Returns dict (empty on failure)."""
    import agent
    hints = []
    dh = dialect_hints(text)
    if dh:
        hints.append("Dialect words found (word -> standard Bangla): " +
                     ", ".join(f"{w} -> {s} ({n})" for n, w, s in dh[:12]))
    dates = date_hints(text)
    if dates:
        hints.append("Dates: " + "; ".join(dates))
    try:
        raw = agent.think_once(UNDERSTAND_PROMPT.format(text=text[:3000], hints="\n".join(hints)), fmt="json")
        m = re.search(r"\{.*\}", raw or "", re.S)
        return json.loads(m.group(0)) if m else {}
    except Exception as e:
        log.warning("understand failed: %s", e)
        return {}


def reply_rule(form, dialect):
    style = CFG.get("bangla_reply", "same")
    if style == "bangla":
        return "Reply in simple standard Bangla (Bangla script)."
    if style == "english":
        return "Reply in English."
    if form == "banglish":
        return ("The user writes Banglish (Bangla in English letters). Reply the same way: Bangla words in English "
                "letters, simple and friendly, like a Bangladeshi friend texting.")
    if form == "dialect" or (dialect and dialect != "none"):
        return (f"The user writes in a regional dialect ({dialect}). Reply in simple, warm standard spoken Bangla "
                "(Bangla script) that they will easily understand; you may use a familiar dialect word or two.")
    if form == "mixed":
        return "The user mixes Bangla and English. Reply the same natural way (Bangla with common English words)."
    return "Reply in simple, natural Bangla (Bangla script), not formal book language."


def prepare(user_text):
    """Returns (text_for_the_brain, extra_system_instructions). English messages pass through unchanged."""
    if not CFG.get("bangla_helper", True) or not needs_help(user_text) or len(user_text.strip()) < 3:
        return user_text, ""
    text = user_text.translate(BN_DIGITS)
    u = understand(text)
    form = (u.get("form") or ("banglish" if is_banglish(text) else "standard bangla")).lower()
    dialect = u.get("dialect") or guess_dialect(text) or "none"
    if form == "english" and not has_bangla_script(text):
        return user_text, ""
    lines = []
    if u.get("english"):
        lines.append(f"Meaning in English: {u['english']}")
    if u.get("standard_bangla") and form != "standard bangla":
        lines.append(f"In standard Bangla: {u['standard_bangla']}")
    dates = date_hints(text)
    if dates:
        lines.append("Dates: " + "; ".join(dates))
    for_brain = user_text + ("\n\n(" + " | ".join(lines) + ")" if lines else "")
    extra = ("LANGUAGE: " + reply_rule(form, dialect) +
             " Understand Bangla, Banglish and all regional dialects; the meaning in English is given after the user's "
             "message to help you. Use English digits for numbers in tool calls." +
             (f" If this is unclear, ask about: {u['unclear']}." if u.get("unclear") else ""))
    return for_brain, extra
