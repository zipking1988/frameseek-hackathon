"""No-dispatch concept alignment for existing captions and structured observations.

A bounded bilingual vocabulary is normalization, not a new visual model. Unknown
conditions are retained for clarification, not ignored. Source-specific facts,
entity bindings and occurrence times always come from the supplied index.
"""

import re

from frameseek.occurrences import Condition, normalized
from frameseek.windows import find_single_time, find_time_range


def primary_item_description(value):
    """Keep item and color while excluding narrowly described secondary details."""
    value = re.sub(
        r"\s+with\s+(?:white|red|blue|yellow|black|green|brown)"
        r"(?:\s*(?:,|\band\b)\s*(?:white|red|blue|yellow|black|green|brown))*"
        r"\s+(?:trim|accents)$",
        "",
        normalized(value),
    )
    return re.sub(r"\s+with\s+(?:the\s+)?number\s+\d{1,3}$", "", value)


# General visual concepts; no video IDs, teams, answers or source timestamps.
VOCABULARY = {
    "white": ["白色", "白", "white"],
    "red": ["红色", "红", "red"],
    "blue": ["蓝色", "蓝", "blue"],
    "yellow": ["黄色", "黄", "yellow"],
    "black": ["黑色", "黑", "black"],
    "green": ["绿色", "绿", "green"],
    "brown": ["棕色", "棕", "褐色", "褐", "brown"],
    "jersey": ["球衣", "jersey", "jerseys"],
    "shirt": ["衬衫", "上衣", "shirt", "shirts"],
    "clothing": ["衣服", "服装", "clothes", "clothing"],
    "hat": ["帽子", "帽", "hat", "cap"],
    "headband": ["头带", "发带", "headband"],
    "text": ["文字", "文本", "text", "writing"],
    "person": ["人物", "球员", "人员", "人", "person", "people", "player", "players"],
    "male": ["男人", "男子", "男性", "man", "men", "male"],
    "female": ["女人", "女子", "女性", "woman", "women", "female"],
    "parcel": ["纸箱", "箱子", "包裹", "parcel", "box", "boxes", "package"],
    "car": ["汽车", "车辆", "car", "cars", "vehicle"],
    "bicycle": ["自行车", "单车", "bicycle", "bike"],
    "basketball": ["篮球", "basketball"],
    "holding": ["拿着", "握着", "手持", "holding"],
}
GRAMMAR = [
    "画面里有",
    "画面中有",
    "画面上有",
    "哪里有",
    "找一个",
    "有哪些",
    "可以帮我列出来",
    "帮我列出来",
    "列出来",
    "知道timeline",
    "知道时间线",
    "出现在哪些时间",
    "在哪些时间出现",
    "什么时候出现",
    "和画面",
    "及画面",
    "在哪些时间",
    "画面证据",
    "列出画面",
    "列出所有出现时间",
    "列出出现时间",
    "列出所有出现的时间",
    "所有出现的时间",
    "所有出现时间",
    "整段视频里面",
    "整段视频里",
    "整段视频",
    "视频里面",
    "视频里",
    "视频中",
    "出现的时间",
    "出现时间",
    "帮我找出",
    "帮我找到",
    "帮我找",
    "找出",
    "有什么",
    "找到",
    "查找",
    "请",
    "穿着",
    "身穿",
    "穿",
    "戴着",
    "戴",
    "的",
    "和",
    "以及",
    "并且",
    "并",
    "与",
    "全部",
    "所有",
    "列出",
    "出现",
    "时间",
    "find all",
    "find",
    "show me",
    "show",
    "list all",
    "list",
    "all appearances",
    "appearances",
    "all times",
    "timestamps",
    "times",
    "in the video",
    "throughout the video",
    "where does",
    "when does",
    "when do",
    "where is",
    "where are",
    "when is",
    "is there",
    "are there",
    "does the",
    "every appearance",
    "appearance",
    "appear",
    "of",
    "video",
    "please",
    "wearing",
    "wears",
    "with",
    "and",
    "a",
    "the",
]

# A single observed garment value may describe multiple colors. Never pair
# colors and items across separate values or multiple garment types.
COLOR_CONCEPTS = {"white", "red", "blue", "yellow", "black", "green", "brown"}
BODY_CLOTHING_CONCEPTS = {"jersey", "shirt", "clothing"}
GARMENT_CONCEPTS = {*BODY_CLOTHING_CONCEPTS, "hat", "headband"}
HELD_OBJECT_CONCEPTS = {"parcel", "car", "bicycle", "basketball"}
KIND_CONCEPTS = {"person", *HELD_OBJECT_CONCEPTS}


def remove_phrase(text, phrase):
    if re.fullmatch(r"[a-z ]+", phrase):
        return re.sub(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", " ", text)
    return text.replace(phrase, " ")


def remove_time_range(question):
    """Remove only a validated range's syntax, leaving the visual conditions."""
    match = find_time_range(question) or find_single_time(question)
    return question[: match.start()] + " " + question[match.end() :] if match else question


def canonical(value):
    value = normalized(value)
    for key, aliases in VOCABULARY.items():
        if value in aliases:
            return key
    return value


def interpret(question):
    text = normalized(question)
    # A bare “找” remains after an explicit quoted-text predicate is removed.
    # Do not strip this character from arbitrary visual descriptions.
    text = re.sub(r"^\s*找(?=[\s，。！？、,:;?.!\-]*$)", " ", text)
    # The question form “有没有” asks whether something exists; it does not
    # negate the requested condition. Preserve actual negations after removal.
    text = text.replace("有没有", " ")
    if re.search(
        r"\b(?:not|no|never|without|or|except)\b|n['’]t\b|没有|不穿|不是|或者|除了|未戴|不戴", text
    ):
        return [], [
            "Negation or alternatives require explicit structured filters; they are not guessed."
        ]
    # Treat sentence-final 吗 and the leading 有 in the corresponding
    # yes/no form as grammar, without discarding 有 inside visual facts.
    if re.search(r"吗[？?。.!！\s]*$", text):
        text = re.sub(r"^\s*有(?=.+吗[？?。.!！\s]*$)", " ", text)
        text = re.sub(r"吗(?=[？?。.!！\s]*$)", " ", text)
    # "A person in a white jersey" describes clothing. Do not remove a
    # general spatial "in": "a person in a room" needs a grounded relation.
    text = re.sub(
        r"\bin\s+(?:(?:a|an|the)\s+)?"
        r"(?=(?:(?:white|red|blue|yellow|black|green|brown)\s+)?"
        r"(?:jerseys?|shirts?|hats?|caps?|headbands?)\b)",
        "wearing ",
        text,
    )
    terms = []
    phrases = sorted(
        ((alias, key) for key, aliases in VOCABULARY.items() for alias in aliases),
        key=lambda pair: -len(pair[0]),
    )
    # Remove question grammar first so e.g. 时间 is not misread as a source fact.
    for phrase in sorted(GRAMMAR, key=len, reverse=True):
        text = remove_phrase(text, phrase)
    for alias, key in phrases:
        changed = remove_phrase(text, alias)
        if changed != text:
            terms.append(key)
            text = changed
    remainder = re.sub(r"[\s，。！？、,:;?.!\-]+", " ", text).strip()
    if remainder:
        # Arbitrary literal index vocabulary remains usable; it is not translated
        # or discarded. Structured indexes below must ground every term.
        terms.extend(remainder.split())
    if set(terms) & {"male", "female"} and "person" not in terms:
        terms.append("person")
    return list(dict.fromkeys(terms)), []


def caption_terms(caption):
    text = normalized(caption)
    found = set()
    for key, aliases in VOCABULARY.items():
        if any(remove_phrase(text, alias) != text for alias in aliases):
            found.add(key)
    return found


def caption_matches(caption, terms):
    concepts = caption_terms(caption)
    literal = normalized(caption)
    return all(term in concepts or term not in VOCABULARY and term in literal for term in terms)


def resolve_conditions(index, question, explicit=None):
    if explicit:
        return [Condition.model_validate(c) for c in explicit], []
    # A quoted literal is searched only within exact text recorded by the
    # extractor. Similar captions or subject descriptions are not OCR evidence.
    quoted = re.search(
        r"(?:文字|文本|text)\s*(?:包含|是|为|contains|[:：])?\s*"
        r'(?:“([^”]{1,200})”|"([^"\n]{1,200})"|‘([^’]{1,200})’)',
        question,
        re.I,
    )
    if quoted:
        remainder = question[: quoted.start()] + " " + question[quoted.end() :]
        terms, problems = interpret(remainder)
        if problems or terms:
            return [], problems or [
                "Search quoted visible text separately to avoid mixing conditions."
            ]
        literal = next(part for part in quoted.groups() if part is not None)
        if not normalized(literal):
            return [], ["Enter the visible text to find."]
        return [Condition(field="text", value=literal, operator="contains")], []
    remainder = normalized(question)
    conditions = []
    # Aliases shipped with the source's trusted observation index can express
    # arbitrary multilingual concepts and relationships, beyond built-in words.
    for alias in sorted(index.query_aliases, key=len, reverse=True):
        changed = remove_phrase(remainder, normalized(alias))
        if changed != remainder:
            conditions.extend(index.query_aliases[alias])
            remainder = changed
    terms, problems = interpret(remainder)
    if problems or not terms and not conditions:
        return [], problems or ["Describe the object, visible attributes or action to find."]
    # Field/value names are extracted from this source's existing observations.
    options = {}
    compounds = {}
    named_pairs = {}
    for observation in index.observations:
        for subject in observation.subjects:
            for field, colors in subject.facts.items():
                if not field.endswith("_color") or len(colors) != 1:
                    continue
                item_field = field.removesuffix("_color")
                items = subject.facts.get(item_field, [])
                if len(items) != 1:
                    continue
                color, item = canonical(colors[0]), canonical(items[0])
                if color in COLOR_CONCEPTS and item in GARMENT_CONCEPTS:
                    named_pairs.setdefault((color, item), set()).add(
                        (field, colors[0], item_field, items[0])
                    )
                    if item in BODY_CLOTHING_CONCEPTS:
                        named_pairs.setdefault((color, "clothing"), set()).add(
                            (field, colors[0], item_field, items[0])
                        )
            for field, values in subject.facts.items():
                for value in values:
                    options.setdefault(canonical(value), set()).add((field, value))
                    words, value_problems = interpret(primary_item_description(value))
                    colors = set(words) & COLOR_CONCEPTS
                    garments = set(words) & GARMENT_CONCEPTS
                    if (
                        not value_problems
                        and colors
                        and len(garments) == 1
                        and set(words) == colors | garments
                    ):
                        for color in colors:
                            item = next(iter(garments))
                            compounds.setdefault((color, item), set()).add((field, value))
                            if item in BODY_CLOTHING_CONCEPTS:
                                compounds.setdefault((color, "clothing"), set()).add((field, value))
    color_aliases = sorted(
        (alias for concept in COLOR_CONCEPTS for alias in VOCABULARY[concept]),
        key=len,
        reverse=True,
    )
    garment_aliases = sorted(
        (alias for concept in GARMENT_CONCEPTS for alias in VOCABULARY[concept]),
        key=len,
        reverse=True,
    )
    color_pattern = "|".join(re.escape(alias) for alias in color_aliases)
    garment_pattern = "|".join(re.escape(alias) for alias in garment_aliases)
    # Bind every color in an explicit conjunction to the same garment value.
    # Separate garment observations cannot establish a multicolored item.
    multi_pattern = re.compile(
        rf"(?<![a-z0-9])((?:{color_pattern})(?:\s*(?:\band\b|和|与|、)\s*"
        rf"(?:{color_pattern}))+?)\s*(?:的\s*)?({garment_pattern})(?![a-z0-9])"
    )
    pair_text = normalized(question)
    consumed = set()
    for match in list(multi_pattern.finditer(pair_text)):
        colors = {canonical(color) for color in re.findall(color_pattern, match.group(1))}
        item = canonical(match.group(2))
        candidates = set.intersection(*(compounds.get((color, item), set()) for color in colors))
        fields = {field for field, _ in candidates}
        if len(colors) >= 2 and len(fields) == 1:
            conditions.append(
                Condition(
                    field=next(iter(fields)),
                    value=" ".join([*sorted(colors), item]),
                    operator="color_item",
                )
            )
            consumed.update(colors | {item})
        else:
            problems.append(
                f"The index has not confirmed that '{match.group(0)}' belongs to the same item."
            )
        pair_text = pair_text.replace(match.group(0), " ", 1)
    pair_pattern = re.compile(
        rf"(?<![a-z0-9])({color_pattern})\s*(?:的\s*)?({garment_pattern})(?![a-z0-9])"
    )
    pairs = pair_pattern.findall(pair_text)
    # Inspect occurrences rather than the deduplicated concept set: a red
    # hat and a red ball contain two distinct color requirements.
    unbound_terms, _ = interpret(pair_pattern.sub(" ", pair_text))
    for color, garment in pairs:
        pair = (canonical(color), canonical(garment))
        candidates = compounds.get(pair, set())
        named = named_pairs.get(pair, set())
        fields = {field for field, _ in candidates} | {item_field for _, _, item_field, _ in named}
        if candidates and named and len(fields) == 1:
            conditions.append(
                Condition(field=next(iter(fields)), value=" ".join(pair), operator="color_item")
            )
            consumed.update(pair)
        elif len(candidates) == 1 and not named:
            field, _ = next(iter(candidates))
            # Preserve uncertainty for other subjects whose item and generic
            # color are separate, rather than treating them as definite misses.
            conditions.append(Condition(field=field, value=" ".join(pair), operator="color_item"))
            consumed.update(pair)
        elif (candidates or named) and len(fields) == 1 and (len(candidates) + len(named)) > 1:
            conditions.append(
                Condition(field=next(iter(fields)), value=" ".join(pair), operator="color_item")
            )
            consumed.update(pair)
        elif not candidates and len(named) == 1:
            _, _, item_field, _ = next(iter(named))
            # Enforce binding for every searched subject, not just the one
            # whose facts made this field discoverable during compilation.
            conditions.append(
                Condition(field=item_field, value=" ".join(pair), operator="color_item")
            )
            consumed.update(pair)
        else:
            problems.append(
                f"The index has not confirmed that '{color}{garment}' belongs to the same item."
            )
    terms = [term for term in terms if term not in consumed]
    # Match the relationship only when one recorded fact explicitly binds the
    # action and object. Separate facts or other subjects cannot be combined.
    held_objects = set(terms) & HELD_OBJECT_CONCEPTS
    if "holding" in terms and held_objects:
        if len(held_objects) != 1 or set(unbound_terms) & COLOR_CONCEPTS:
            problems.append("Specify the object and attributes; their association is unconfirmed.")
        else:
            held_object = next(iter(held_objects))
            relations = set()
            for observation in index.observations:
                for subject in observation.subjects:
                    for field in ("action", "relationship"):
                        for value in subject.facts.get(field, []):
                            concepts, issues = interpret(value)
                            if not issues and set(concepts) == {"holding", held_object}:
                                relations.add((field, value))
            if len(relations) == 1:
                field, value = next(iter(relations))
                conditions.append(Condition(field=field, value=value))
                terms = [term for term in terms if term not in {"holding", held_object}]
            else:
                problems.append("The index has not uniquely confirmed who holds the object.")
    indexed_fields = {field for values in options.values() for field, _ in values}
    for term in terms:
        available = options.get(term, set())
        fields = {field for field, _ in available}
        # A known category or object color can be searched even if no instance
        # was observed. This is only valid when the index actually uses the
        # corresponding generic field; the result still carries unknown coverage.
        if not fields and term in KIND_CONCEPTS and "kind" in indexed_fields:
            fields = {"kind"}
        if (
            not fields
            and term in COLOR_CONCEPTS
            and set(terms) & HELD_OBJECT_CONCEPTS
            and "color" in indexed_fields
        ):
            fields = {"color"}
        if len(fields) != 1:
            problems.append(
                f"The index cannot uniquely interpret '{term}'; select an available filter."
            )
        else:
            conditions.append(Condition(field=next(iter(fields)), value=term))
    unique = {(c.field, c.value, c.operator): c for c in conditions}
    if len(unique) > 12:
        return [], ["More than 12 conditions; narrow the question."]
    return list(unique.values()), problems


def available_conditions(index):
    return [
        {"field": field, "value": value, "operator": "is"}
        for field, value in sorted(
            {
                (field, value)
                for o in index.observations
                for s in o.subjects
                for field, values in s.facts.items()
                for value in values
            }
        )
    ][:200]
