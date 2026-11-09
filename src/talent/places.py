"""Places a search box can understand: "backend engineer Boston" is a role and a place.

Nothing here touches the database. `split_query` pulls a known place (a city, a state or a metro area) out of a
typed search and hands back the role words that are left; `Place.matches` says whether a saved location (the text
in a person's Location field) is that place. A place is only taken when the words match a name on the lists below,
so an ordinary word is never mistaken for one.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_STATE_BY_NAME = {v.lower(): k for k, v in STATES.items()}

# city -> state
CITIES = {
    "boston": "MA", "cambridge": "MA", "somerville": "MA", "waltham": "MA", "worcester": "MA",
    "austin": "TX", "dallas": "TX", "houston": "TX", "san antonio": "TX", "plano": "TX", "fort worth": "TX",
    "new york": "NY", "new york city": "NY", "brooklyn": "NY", "manhattan": "NY", "buffalo": "NY",
    "rochester": "NY", "albany": "NY", "san francisco": "CA", "oakland": "CA", "san jose": "CA",
    "palo alto": "CA", "berkeley": "CA", "mountain view": "CA", "sunnyvale": "CA", "menlo park": "CA",
    "redwood city": "CA", "santa clara": "CA", "fremont": "CA", "los angeles": "CA", "san diego": "CA",
    "sacramento": "CA", "irvine": "CA", "santa monica": "CA", "pasadena": "CA", "seattle": "WA",
    "bellevue": "WA", "redmond": "WA", "spokane": "WA", "portland": "OR", "eugene": "OR", "denver": "CO",
    "boulder": "CO", "chicago": "IL", "evanston": "IL", "atlanta": "GA", "miami": "FL", "orlando": "FL",
    "tampa": "FL", "jacksonville": "FL", "phoenix": "AZ", "tempe": "AZ", "scottsdale": "AZ", "tucson": "AZ",
    "las vegas": "NV", "salt lake city": "UT", "provo": "UT", "minneapolis": "MN", "saint paul": "MN",
    "st paul": "MN", "detroit": "MI", "ann arbor": "MI", "columbus": "OH", "cleveland": "OH", "cincinnati": "OH",
    "pittsburgh": "PA", "philadelphia": "PA", "baltimore": "MD", "washington": "DC", "arlington": "VA",
    "richmond": "VA", "raleigh": "NC", "durham": "NC", "charlotte": "NC", "nashville": "TN", "memphis": "TN",
    "new orleans": "LA", "kansas city": "MO", "st louis": "MO", "saint louis": "MO", "indianapolis": "IN",
    "milwaukee": "WI", "madison": "WI", "omaha": "NE", "providence": "RI", "hartford": "CT",
    "new haven": "CT", "stamford": "CT", "newark": "NJ", "jersey city": "NJ", "princeton": "NJ",
    "honolulu": "HI", "anchorage": "AK", "boise": "ID", "albuquerque": "NM", "oklahoma city": "OK",
    "louisville": "KY", "birmingham": "AL", "charleston": "SC", "savannah": "GA", "champaign": "IL",
    "urbana": "IL", "san mateo": "CA", "cupertino": "CA", "oakland": "CA",
    "toronto": "", "vancouver": "", "montreal": "", "london": "", "dublin": "", "berlin": "", "paris": "",
    "amsterdam": "", "bangalore": "", "bengaluru": "", "singapore": "", "sydney": "", "tel aviv": "",
}
BAY_AREA = ("san francisco", "oakland", "san jose", "palo alto", "berkeley", "mountain view", "sunnyvale",
            "menlo park", "redwood city", "santa clara", "fremont", "san mateo", "cupertino")
# Names that mean a group of places (a metro area), or another spelling of one place.
METROS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bay area": ("the Bay Area", BAY_AREA),
    "sf bay area": ("the Bay Area", BAY_AREA),
    "silicon valley": ("Silicon Valley", ("palo alto", "mountain view", "sunnyvale", "menlo park", "redwood city",
                                          "santa clara", "san jose", "cupertino")),
    "greater boston": ("Greater Boston", ("boston", "cambridge", "somerville", "waltham")),
    "nyc": ("New York, NY", ("new york", "brooklyn", "manhattan")),
    "new york city": ("New York, NY", ("new york", "brooklyn", "manhattan")),
    "sf": ("San Francisco, CA", ("san francisco",)),
    "sfo": ("San Francisco, CA", ("san francisco",)),
    "la": ("Los Angeles, CA", ("los angeles",)),
    "dc": ("Washington, DC", ("washington", "district of columbia")),
}
# Single words people also use as first names or ordinary words: a place only when something else says so (a
# state after it, or "in" before it), and never when the typed words are a saved person's name.
_AMBIGUOUS = {"austin", "dallas", "charlotte", "phoenix", "paris", "madison", "savannah", "london", "boulder",
              "washington", "georgia", "virginia", "orlando", "houston", "florence", "jackson", "eugene", "la"}
_FILLER = {"in", "near", "around", "from", "based", "at", "the", "area", "of", "who", "live", "lives", "living",
           "located", "people", "someone", "candidates", "candidate", "find", "me", "show"}


@dataclass(frozen=True)
class Place:
    label: str  # "Boston, MA"
    words: tuple[str, ...]  # city names (lower case) a location may have
    state: str = ""  # "MA", when the search gave one or the city has one
    state_only: bool = False  # the search named a state or a whole country, not a city
    strict_state: bool = False  # the search typed the state ("Boston, MA"): a location in another state is not it

    def matches(self, location: str | None) -> bool:
        text = _fold(location or "")
        if not text:
            return False
        if self.state_only:
            return _state_in(text, self.state)
        if not any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in self.words):
            return False
        if self.strict_state and self.state:
            other = _states_in(text)
            if other and self.state not in other:
                return False
        return True


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def _states_in(text: str) -> set[str]:
    found = {code for name, code in _STATE_BY_NAME.items() if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text)}
    found |= {m.upper() for m in re.findall(r"(?:,|\s)\s*([a-z]{2})\s*(?:,|$|\d)", text) if m.upper() in STATES}
    return found


def _state_in(text: str, code: str) -> bool:
    return code in _states_in(text)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", _fold(text))


def split_query(query: str) -> tuple[str, Place | None]:
    """(the role words that are left, the place found) for a typed search, or (query, None) when it names no
    place or nothing is left but the place. "backend engineer in Boston, MA" gives ("backend engineer", Boston, MA)."""
    raw = query or ""
    if not raw.strip() or any(ch in raw for ch in '":()') or re.search(r"(?<!\w)(?:OR|NOT|NEAR(?:/\d+)?)(?!\w)|(?<!\w)-\w", raw):
        return query, None  # the search box's own operators are left alone
    toks = _words(raw)
    low = " ".join(toks)
    if "@" in raw or "http" in low or len(toks) < 2 or len(toks) > 12:
        return query, None
    best: tuple[int, int, Place, str] | None = None  # (start, end, place, typed)
    for n in (3, 2, 1):  # the longest name first, the last one in the query
        for i in range(len(toks) - n, -1, -1):
            name = " ".join(toks[i:i + n])
            place = _lookup(name)
            if place is None:
                continue
            j = i + n
            typed_state = ""
            if j < len(toks) and toks[j].upper() in STATES and not place.state_only:  # "Boston, MA" / "Boston MA"
                if toks[j].upper() == place.state or not place.state:
                    typed_state = toks[j].upper()
                    j += 1
            elif j + 1 < len(toks) and not place.state_only and _STATE_BY_NAME.get(" ".join(toks[j:j + 2])):
                typed_state = _STATE_BY_NAME[" ".join(toks[j:j + 2])]
                j += 2
            elif j < len(toks) and not place.state_only and _STATE_BY_NAME.get(toks[j]):
                typed_state = _STATE_BY_NAME[toks[j]]
                j += 1
            if typed_state and place.state and typed_state != place.state and place.words[0] not in ("washington",):
                continue
            lead = toks[i - 1] if i else ""
            if n == 1 and name in _AMBIGUOUS and not typed_state and len(toks) - n < 2 \
                    and lead not in ("in", "near", "around", "from"):
                continue
            if len(name) == 2 and name.upper() in STATES and not place.state_only:
                continue
            if typed_state:
                place = Place(f"{place.label.split(',')[0]}, {typed_state}", place.words, typed_state,
                              strict_state=True) if not place.state_only else place
            if best is None or j - i > best[1] - best[0]:
                best = (i, j, place, name)
        if best:
            break
    if best is None:
        return query, None
    i, j, place, _name = best
    role = [t for k, t in enumerate(toks) if not (i <= k < j)]
    while role and role[-1] in _FILLER:
        role.pop()
    while role and role[0] in _FILLER:
        role.pop(0)
    role = [t for t in role if t not in _FILLER] if len(role) > 1 else role
    if not role or all(t in _FILLER for t in role):
        return query, None
    return " ".join(role), place


def _lookup(name: str) -> Place | None:
    if name in METROS:
        label, cities = METROS[name]
        return Place(label, cities)
    if name in CITIES:
        state = CITIES[name]
        label = name.title() + (f", {state}" if state else "")
        return Place(label, (name,), state)
    if name in _STATE_BY_NAME:
        code = _STATE_BY_NAME[name]
        return Place(STATES[code] + f" ({code})", (name,), code, state_only=True)
    if len(name) == 2 and name.upper() in STATES and name not in ("in", "or", "me", "hi", "ok", "id", "pa", "la", "de"):
        return Place(STATES[name.upper()] + f" ({name.upper()})", (name,), name.upper(), state_only=True)
    return None
