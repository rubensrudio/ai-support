"""Bilingual guardrail patterns (CT-52, GRD-09, GRD-90). Pure regex, no LLM.

- `INJECTION_PATTERNS[lang]`: prompt-injection attempts in en and pt ("ignore previous
  instructions", "ignore as instruções anteriores", "you are now", "system prompt", "revele o
  prompt", requests for other customers' data...). Anchored so legitimate support phrases do not
  match (e.g. "Please ignore the duplicate charge", "ignore a cobrança duplicada").
- `ACTION_PROMISE_PATTERNS[tool][lang]`: sentences that claim an action of `tool` was done
  ("I have cancelled your order", "seu reembolso foi emitido"...), used by the output guard.
- `find_injection(text)`: names of the injection patterns that match `text` in any language
  (empty list = clean). Case- and accent-insensitive.

Every pattern is compiled with `re.IGNORECASE` and is accent tolerant (an unaccented vowel or
`c` in the source also matches its accented forms, `'` also matches `’`), so it works both on
raw text and on the accent-stripped text `find_injection` scans. Each injection pattern has a
single named group whose name is the stable pattern name reported by `find_injection`.
"""

import re
import unicodedata

from api.messages import Lang

_ACCENTS = {
    "a": "aáàâãä",
    "e": "eéèêë",
    "i": "iíìîï",
    "o": "oóòôõö",
    "u": "uúùûü",
    "c": "cç",
    "'": "'’",
}


def _accent_tolerant(source: str) -> str:
    """Rewrites letters outside escapes, classes and group headers as accent-tolerant classes."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "\\":
            out.append(source[i : i + 2])
            i += 2
        elif ch == "[":
            end = i + 1
            if end < n and source[end] == "]":
                end += 1
            while end < n and source[end] != "]":
                end += 2 if source[end] == "\\" else 1
            out.append(source[i : end + 1])
            i = end + 1
        elif ch == "{":
            end = source.index("}", i)
            out.append(source[i : end + 1])
            i = end + 1
        elif source.startswith("(?P<", i):
            end = source.index(">", i)
            out.append(source[i : end + 1])
            i = end + 1
        elif source.startswith(("(?<!", "(?<="), i):
            out.append(source[i : i + 4])
            i += 4
        elif source.startswith(("(?:", "(?!", "(?="), i):
            out.append(source[i : i + 3])
            i += 3
        elif source.startswith("(?", i):
            end = source.index(":", i)
            out.append(source[i : end + 1])
            i = end + 1
        elif ch.lower() in _ACCENTS:
            out.append(f"[{_ACCENTS[ch.lower()]}]")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _compile(source: str) -> re.Pattern[str]:
    return re.compile(_accent_tolerant(source), re.IGNORECASE)


def _named(name: str, source: str) -> re.Pattern[str]:
    return _compile(f"(?P<{name}>{source})")


# A clause ends at `.`, `;`, `:`, `!` or `?` followed by a space (so "$49.90" or "#1.001" do not
# end it) or at a line break.
_CLAUSE_START = r"(?:^|(?<=[.;:!?]\s)|(?<=\n))"
_CLAUSE_CHAR = r"(?![.;:!?](?:\s|$))[^\n]"


def _clause_prefix_without(words: str) -> str:
    """From the start of the clause, any text in which none of `words` starts.

    Placed before a pattern, the pattern only matches when none of `words` appears earlier in the
    same clause ("If your order #1 was cancelled" cannot match from "your" or from "order").
    """
    return rf"{_CLAUSE_START}(?:(?!\b(?:{words})\b){_CLAUSE_CHAR})*?"


def _not_followed_in_clause_by(words: str) -> str:
    """Negative lookahead: none of `words` follows in the rest of the clause, before a comma."""
    return rf"(?!(?:(?!,){_CLAUSE_CHAR})*?\b(?:{words})\b)"


# A thematic complement turns "your rules" into a support question ("your rules for refunds",
# "suas regras de reembolso"); "of the system" / "do sistema" keeps it an attack.
_EN_NO_TOPIC = (
    r"(?!\s+(?:about|for|on|regarding|concerning|re|to|when)\b"
    r"|\s+of\s+(?!the\s+system\b|your\s+system\b))"
)
_PT_NO_TOPIC = (
    r"(?!\s+(?:sobre|para|pra|quanto|em|no|na|nos|nas)\b"
    r"|\s+(?:de|do|da|dos|das)\s+(?!sistema\b|seguranca\b))"
)
# A first-person report of a leak ("I can see orders from another customer") is not a request.
_EN_REPORT = (
    r"i\s+(?:can\s+)?see|i'm\s+seeing|i\s+am\s+seeing|i\s+saw|i\s+(?:just\s+)?(?:found|"
    r"received|got|noticed)|(?:is|are)\s+showing|shows?\s+up|showing\s+up|appears?|appeared|"
    r"visible|mixed"
)
_PT_REPORT = (
    r"vejo|vendo|vi|aparece|aparecem|apareceu|apareceram|aparecendo|recebi|encontrei|achei|"
    r"notei|consta|constam|mostrando|misturad[oa]s?"
)


# --- Injection: English -----------------------------------------------------------------------

_EN_IGNORE_VERB = r"\b(?:ignore|forget|disregard|skip|override|drop|discard)"
_EN_QUALIFIER = (
    r"(?:previous|prior|above|earlier|preceding|former|original|initial|system|old|existing)"
)
_EN_DIRECTIVE = r"(?:instructions?|prompts?|rules|guidelines|directives|commands|context)"
_EN_PRIV_ROLE = (
    r"(?:admin(?:istrator)?|sys-?admin|developer|dev|root|super-?user|system|operator|"
    r"supervisor|manager|ceo|god|staff|employee|hacker|moderator|"
    r"unrestricted|unfiltered|jailbroken|uncensored|dan)\b"
)
_EN_PEOPLE = r"(?:customers?|users?|clients?|subscribers?|account\s+holders?)"
_EN_DATA = (
    r"(?:personal\s+|private\s+|contact\s+)?(?:data|e-?mails?|e-?mail\s+addresses|addresses|"
    r"orders?|order\s+history|information|info|details|records|phone(?:\s+numbers?)?|names|"
    r"cards?|payments?|accounts?|subscriptions?)"
)
_EN_SPECIFIC_CUSTOMER = (
    r"(?:customer|client|user|subscriber)\s+"
    r"(?:named\s+\w+|called\s+\w+|with\s+(?:the\s+)?(?:id|e-?mail|number)\s+\S+|"
    r"(?:id|number|no\.?|#)\s*:?\s*\d+|#?\d{2,}|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\[EMAIL\])"
)

_EN_REVEAL_VERB = (
    r"\b(?:reveal|show|print|display|repeat|output|tell|share|leak|dump|give|write|recite|"
    r"paste|copy|spell\s+out|translate|summari[sz]e|rewrite|paraphrase)"
)

_EN_INJECTION = (
    _named(
        "ignore_instructions",
        rf"{_EN_IGNORE_VERB}\s+(?:all\s+(?:of\s+)?|any\s+(?:of\s+)?)?"
        rf"(?:the\s+|your\s+|these\s+|those\s+)?{_EN_QUALIFIER}\s+{_EN_DIRECTIVE}\b"
        rf"|{_EN_IGNORE_VERB}\s+(?:all\s+(?:of\s+)?)?your\s+"
        rf"(?:instructions|rules|guidelines|directives|programming|prompt|training)\b{_EN_NO_TOPIC}"
        rf"|{_EN_IGNORE_VERB}\s+all\s+(?:the\s+|of\s+the\s+)?(?:instructions|rules)\b"
        rf"{_EN_NO_TOPIC}"
        rf"|{_EN_IGNORE_VERB}\s+(?:everything|all|anything)\s+"
        r"(?:above|before|previously|you\s+(?:were|have\s+been)\s+told)\b",
    ),
    _named(
        "disregard_rules",
        r"\b(?:disregard|ignore|bypass|override|break|forget|circumvent|skip)\s+"
        r"(?:all\s+(?:of\s+)?)?(?:the\s+|your\s+|any\s+)?"
        r"(?:rules|guardrails|restrictions|safety\s+(?:rules|filters|guidelines|measures)|"
        rf"safety|content\s+polic(?:y|ies)|filters|limitations)\b{_EN_NO_TOPIC}"
        r"|\b(?:bypass|circumvent)\s+(?:all\s+)?(?:the\s+|your\s+)?(?:\w+\s+)?"
        rf"(?:guidelines|policies|protections|checks)\b{_EN_NO_TOPIC}",
    ),
    _named(
        "role_override",
        r"\byou\s+are\s+now\s+(?:a|an|in|the|no\s+longer|free|unrestricted|unfiltered|"
        r"jailbroken|dan|called|named|acting|operating|going\s+to)\b"
        r"|\byou're\s+now\s+(?:a|an|in|the|no\s+longer|free|unrestricted|unfiltered|dan)\b"
        r"|\bfrom\s+now\s+on\s*,?\s+you\s+(?:are|will|must|shall|act|respond|answer|reply)\b"
        r"|\byou\s+are\s+going\s+to\s+(?:be|act\s+as|pretend)\s+(?:a\s+|an\s+)?"
        r"(?:dan|unrestricted|unfiltered|jailbroken)\b"
        r"|\bnew\s+(?:instructions|rules|persona)\s*:",
    ),
    _named(
        "system_prompt",
        r"\bsystem\s+prompts?\b"
        r"|\b(?:hidden|internal|developer|initial|original|secret|pre-?)\s*prompts?\b"
        r"|\b(?:hidden|internal|developer|secret|system)\s+instructions\b"
        r"|\b(?:your|the\s+hidden|the\s+internal)\s+system\s+message\b",
    ),
    _named(
        "reveal_instructions",
        rf"{_EN_REVEAL_VERB}\s+(?:me\s+|us\s+)?(?:all\s+(?:of\s+)?)?(?:the\s+)?(?:your\s+)?"
        r"(?:hidden|secret|internal|initial|original|confidential|system)\s+"
        r"(?:\w+\s+)?(?:instructions|prompt|rules|configuration|config|guidelines|directives)\b"
        rf"|{_EN_REVEAL_VERB}\s+(?:me\s+|us\s+)?(?:all\s+(?:of\s+)?)?your\s+"
        r"(?:full\s+|exact\s+|complete\s+)?"
        r"(?:instructions|prompt|rules|configuration|config|guidelines|directives)\b"
        rf"{_EN_NO_TOPIC}"
        r"|\b(?:reveal|show|print|display|repeat|output|leak|dump)\s+(?:me\s+)?(?:the\s+)?"
        r"prompt\b"
        r"|\bwhat\s+(?:are|were)\s+your\s+(?:\w+\s+)?(?:instructions|directives|prompts?)\b"
        rf"{_EN_NO_TOPIC}"
        r"|\b(?:print|repeat|output|show|reveal|copy|write|dump)\s+(?:me\s+)?(?:the\s+|all\s+the\s+)?"
        r"(?:text|words|content|messages?)\s+(?:above|before)\b"
        r"|\bwhat\s+(?:was|is)\s+written\s+(?:above|before)\b",
    ),
    _named(
        "jailbreak_mode",
        r"\b(?:enable|enables|activate|enter|switch\s+(?:to|into)|turn\s+on|go\s+into|unlock|"
        r"start|in|use|now)\s+(?:the\s+|your\s+)?(?:developer|dev|god|jailbreak|jailbroken|"
        r"unrestricted|unfiltered|uncensored|sudo|evil)\s+mode\b"
        r"|\b(?-i:D[A]N)\b|\bdo\s+anything\s+now\b|\bjailbr(?:eak|oken)\b"
        r"|\b(?:assistant|ai|model|bot|chatbot)\s+(?:with\s+no|without(?:\s+any)?)\s+"
        r"(?:restrictions|filters|limits|rules|guardrails)\b"
        r"|\bunrestricted\s+(?:assistant|ai|model|bot|chatbot)\b",
    ),
    _named(
        "privileged_role",
        r"\b(?:act|behave|respond|answer|pose|roleplay|role-play|operate)\s+(?:as|like)\s+"
        r"(?:if\s+you\s+(?:were|are)\s+)?(?:an?\s+|the\s+|your\s+)?(?:\w+\s+)?"
        rf"{_EN_PRIV_ROLE}"
        r"|\bpretend\s+(?:to\s+be|you\s+are|you're|that\s+you\s+are)\s+"
        rf"(?:an?\s+|the\s+)?(?:\w+\s+)?{_EN_PRIV_ROLE}"
        rf"|\bimpersonate\s+(?:an?\s+|the\s+)?(?:\w+\s+)?{_EN_PRIV_ROLE}",
    ),
    _named(
        "other_customers_data",
        _clause_prefix_without(_EN_REPORT) + r"(?:(?<!\bto\s)(?<!\bfor\s)(?<!\binto\s)"
        r"\b(?:other|another|different|every|each|all(?:\s+the)?|all\s+other)\s+"
        rf"{_EN_PEOPLE}(?:'s|')?\s+{_EN_DATA}\b"
        rf"|\beveryone(?:\s+else)?'s\s+{_EN_DATA}\b"
        rf"|\b{_EN_DATA}\s+(?:of|for|from|belonging\s+to)\s+"
        r"(?:all\s+(?:of\s+)?(?:the\s+)?|every\s+|other\s+|another\s+|the\s+other\s+|"
        rf"different\s+|any\s+other\s+){_EN_PEOPLE}\b"
        rf"|\b{_EN_DATA}\s+(?:of|for|from|belonging\s+to)\s+(?:the\s+)?{_EN_SPECIFIC_CUSTOMER}"
        r"|\b(?:list|dump|export|show\s+me|give\s+me|print|send\s+me)\s+"
        rf"(?:all|every|the\s+other|other|all\s+the\s+other)\s+(?:the\s+)?{_EN_PEOPLE}\b)",
    ),
    _named(
        "fake_system_tag",
        r"<\s*/?\s*(?:system|instructions?|sys)\s*>"
        r"|\[\s*/?\s*(?:system|inst|sys)\s*\]"
        r"|(?:^|\n)\s*#*\s*(?:system|developer)\s*(?:message|override)?\s*:",
    ),
)

# --- Injection: Portuguese --------------------------------------------------------------------

_PT_IGNORE_VERB = (
    r"\b(?:ignore|ignora|ignorar|ignorem|esqueca|esquece|esquecer|esquecam|desconsidere|"
    r"desconsidera|desconsiderar|despreze|despreza|desprezar|descarte|descarta|abandone|"
    r"anule|sobrescreva)"
)
_PT_DIRECTIVE = (
    r"(?:instrucoes|instrucao|regras|diretrizes|ordens|comandos|prompts?|orientacoes|"
    r"restricoes|programacao|treinamento)"
)
_PT_AFTER = (
    r"(?:anteriores|anterior|previas|acima|originais|iniciais|antigas|do\s+sistema|"
    r"que\s+(?:te|lhe|voce)\s+(?:deram|passaram|recebeu|foram\s+dadas))"
)
_PT_PRIV_ROLE = (
    r"(?:administrador(?:a)?|admin|desenvolvedor(?:a)?|dev|root|superusuario|operador(?:a)?|"
    r"supervisor(?:a)?|gerente|sistema|hacker|moderador(?:a)?|funcionario|"
    r"irrestrit[oa]|sem\s+restricoes|sem\s+filtros?|dan)\b"
)
_PT_PEOPLE = r"(?:clientes?|usuari[oa]s?|assinantes?|contas?|consumidor(?:es)?)"
_PT_DATA = (
    r"(?:dados|e-?mails?|enderecos(?:\s+de\s+e-?mail)?|pedidos|informacoes|telefones|nomes|"
    r"contas|cartoes|historicos?|lista|cadastros?|registros|cpfs?|assinaturas)"
)
_PT_SPECIFIC_CUSTOMER = (
    r"(?:clientes?|usuari[oa]|assinante|conta)\s+"
    r"(?:chamad[oa]\s+\w+|de\s+(?:id|numero|e-?mail|nome)\s+\S+|"
    r"(?:id|numero|n[o.º]|#)\s*:?\s*\d+|#?\d{2,}|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\[EMAIL\])"
)
_PT_REVEAL_VERB = (
    r"\b(?:revele|revela|revelar|mostre|mostra|mostrar|exiba|exibe|exibir|imprima|imprime|"
    r"imprimir|diga|diz|dizer|conte|conta|contar|repita|repete|repetir|copie|copia|liste|"
    r"vaze|vaza|escreva|escreve|compartilhe|me\s+de|me\s+passe|me\s+mostre|me\s+diga)"
)

_PT_INJECTION = (
    _named(
        "ignore_instructions",
        rf"{_PT_IGNORE_VERB}\s+(?:tod[oa]s\s+)?(?:as\s+|os\s+|suas\s+|seus\s+|tuas\s+|teus\s+)?"
        rf"{_PT_DIRECTIVE}\s+(?:\w+\s+)?{_PT_AFTER}"
        rf"|{_PT_IGNORE_VERB}\s+(?:tod[oa]s\s+)?(?:as\s+|os\s+)?(?:suas|seus|tuas|teus)\s+"
        rf"{_PT_DIRECTIVE}\b{_PT_NO_TOPIC}"
        rf"|{_PT_IGNORE_VERB}\s+tod[oa]s\s+(?:as\s+|os\s+)?{_PT_DIRECTIVE}\b{_PT_NO_TOPIC}"
        rf"|{_PT_IGNORE_VERB}\s+(?:tudo|todo\s+o\s+resto)\s+"
        r"(?:acima|antes|anterior|o\s+que\s+(?:te|lhe)\s+(?:disseram|falaram|passaram))\b"
        rf"|{_PT_IGNORE_VERB}\s+o\s+que\s+(?:te|lhe)\s+(?:disseram|falaram|passaram|ensinaram|"
        r"mandaram|instruiram)\b",
    ),
    _named(
        "disregard_rules",
        r"\b(?:desconsidere|desconsidera|desconsiderar|ignore|ignora|ignorar|burle|burla|"
        r"burlar|quebre|quebra|quebrar|esqueca|esquece|viole|viola|contorne|contorna|"
        r"contornar|desative|desativa|desligue|desliga)\s+(?:todas\s+)?(?:as\s+|os\s+|suas\s+|"
        r"seus\s+)?(?:regras|restricoes|diretrizes|politicas\s+de\s+seguranca|filtros|"
        rf"protecoes|travas|limitacoes|salvaguardas|guardrails)\b{_PT_NO_TOPIC}",
    ),
    _named(
        "role_override",
        r"\b(?:agora\s+voce\s+(?:e|eh|sera)|voce\s+agora\s+(?:e|eh|sera))\s+"
        r"(?:um|uma|o|a|meu|minha|livre|irrestrit[oa]|dan)\b"
        r"|\b(?:a\s+partir\s+de\s+agora|de\s+agora\s+em\s+diante)\s*,?\s+voce\s+"
        r"(?:e|eh|sera|vai|deve|devera|responde|age|atua)\b"
        r"|\bnovas\s+(?:instrucoes|regras)\s*:",
    ),
    _named(
        "system_prompt",
        r"\bprompts?\s+(?:do|de)\s+sistema\b"
        r"|\bprompts?\s+(?:inicial|iniciais|original|originais|oculto|ocultos|interno|internos|"
        r"secreto|secretos)\b"
        r"|\binstrucoes\s+(?:ocultas|internas|secretas|do\s+sistema)\b"
        r"|\bsystem\s+prompts?\b",
    ),
    _named(
        "reveal_instructions",
        rf"{_PT_REVEAL_VERB}(?:-me)?\s+(?:me\s+|pra\s+mim\s+|para\s+mim\s+)?(?:tod[oa]s\s+)?"
        r"(?:as\s+|o\s+|os\s+|a\s+)?(?:suas\s+|seu\s+|seus\s+|sua\s+|tuas\s+|teu\s+|teus\s+)?"
        r"(?:instrucoes|regras|diretrizes|prompt|configuracoes|ordens|programacao)\s+"
        r"(?:\w+\s+)?(?:ocultas?|ocultos?|internas?|internos?|secretas?|secretos?|do\s+sistema|"
        r"originais|iniciais|completas?|completos?|confidenciais)\b"
        rf"|{_PT_REVEAL_VERB}(?:-me)?\s+(?:me\s+|pra\s+mim\s+|para\s+mim\s+)?(?:tod[oa]s\s+)?"
        r"(?:as\s+|o\s+|os\s+|a\s+)?(?:suas|seu|seus|sua|tuas|teu|teus)\s+"
        r"(?:instrucoes|regras|diretrizes|prompt|configuracoes|ordens|programacao)\b"
        rf"{_PT_NO_TOPIC}"
        rf"|{_PT_REVEAL_VERB}(?:-me)?\s+(?:o\s+)?prompt\b"
        r"|\bquais\s+(?:sao|foram)\s+(?:as\s+)?(?:suas|tuas)\s+(?:\w+\s+)?"
        rf"(?:instrucoes|ordens|diretrizes)\b{_PT_NO_TOPIC}",
    ),
    _named(
        "jailbreak_mode",
        r"\b(?:ative|ativa|ativar|habilite|habilita|ligue|liga|entre\s+(?:no|em)|entrar\s+(?:no|em)|"
        r"mude\s+para|use|usar|em|agora\s+no)\s+(?:o\s+)?modo\s+(?:desenvolvedor|dev|deus|"
        r"irrestrito|sem\s+restricoes|sem\s+filtros?|jailbreak|root|sudo|god|developer|livre)\b"
        r"|\b(?-i:D[A]N)\b|\bfaca\s+qualquer\s+coisa\s+agora\b|\bjailbr(?:eak|oken)\b"
        r"|\b(?:assistente|ia|modelo|bot|chatbot|robo)\s+(?:\w+\s+)?sem\s+(?:nenhuma\s+|"
        r"qualquer\s+)?(?:restricoes|restricao|filtros?|limites|regras|censura)\b",
    ),
    _named(
        "privileged_role",
        r"\b(?:finja|finge|fingir|aja|age|agir|atue|atua|atuar|comporte-se|se\s+comporte|"
        r"responda|faca\s+de\s+conta|faz\s+de\s+conta|interprete|personifique|banque)\s+"
        r"(?:ser|que\s+(?:e|eh|voce\s+e|voce\s+eh)|como(?:\s+se\s+fosse)?|de|o\s+papel\s+de)\s+"
        rf"(?:um\s+|uma\s+|o\s+|a\s+|meu\s+|minha\s+)?(?:\w+\s+)?{_PT_PRIV_ROLE}",
    ),
    _named(
        "other_customers_data",
        _clause_prefix_without(_PT_REPORT)
        + rf"(?:\b{_PT_DATA}\s+(?:\w+\s+){{0,3}}?(?:de|do|da|dos|das)\s+"
        r"(?:outr[oa]s?|tod[oa]s\s+[oa]s|tod[oa]s\s+[oa]s\s+outr[oa]s|demais|um\s+outro|"
        rf"uma\s+outra|qualquer\s+outr[oa]|cada)\s+{_PT_PEOPLE}\b"
        rf"|\b{_PT_DATA}\s+(?:de|do|da|dos|das)\s+(?:o\s+|a\s+)?{_PT_SPECIFIC_CUSTOMER}"
        r"|\b(?:liste|listar|lista|exporte|exportar|mostre|mostrar|me\s+mostre|me\s+de|"
        r"me\s+passe|imprima|envie)(?:-me)?\s+(?:tod[oa]s\s+[oa]s|[oa]s\s+outr[oa]s|outr[oa]s|"
        rf"[oa]s\s+demais|demais)\s+{_PT_PEOPLE}\b)",
    ),
    _named(
        "fake_system_tag",
        r"<\s*/?\s*(?:sistema|system|instrucoes|instrucao|sys)\s*>"
        r"|\[\s*/?\s*(?:sistema|system|inst|sys)\s*\]"
        r"|(?:^|\n)\s*#*\s*(?:sistema|mensagem\s+do\s+sistema)\s*:",
    ),
)

INJECTION_PATTERNS: dict[Lang, tuple[re.Pattern[str], ...]] = {
    "en": _EN_INJECTION,
    "pt": _PT_INJECTION,
}

# --- Detection --------------------------------------------------------------------------------

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})
_SPACES = re.compile(r"[^\S\n]+")


_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_LEET = re.compile(r"(?<=[A-Za-z])[013457@$](?=[A-Za-z])")
_LEET_MAP = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
_SPACED = re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b")


def _normalize(text: str) -> str:
    """NFKD, drop accents and invisible format chars, unify apostrophes and inline spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(
        ch for ch in decomposed if unicodedata.category(ch) not in {"Mn", "Cf", "Me", "Mc"}
    )
    return _SPACES.sub(" ", kept.translate(_APOSTROPHES))


def _deobfuscate(text: str) -> str:
    """Undo cheap obfuscation for injection scans: CamelCase, l33t letters, s p a c e d words."""
    text = _CAMEL.sub(" ", text)
    text = _LEET.sub(lambda m: _LEET_MAP[m.group()], text)
    return _SPACED.sub(lambda m: m.group().replace(" ", ""), text)


def _name(pattern: re.Pattern[str]) -> str:
    return next(iter(pattern.groupindex))


def find_injection(text: str) -> list[str]:
    normalized = _normalize(text)
    variants = [normalized, _deobfuscate(normalized)]
    names: list[str] = []
    for lang in ("en", "pt"):
        for pattern in INJECTION_PATTERNS[lang]:
            name = _name(pattern)
            if name not in names and any(pattern.search(v) for v in variants):
                names.append(name)
    return names


# --- Action promises (claims that an action is already done) ---------------------------------
#
# A promise is a statement that an action on THIS customer's order/refund/e-mail is done: the
# agent in the first person ("I have cancelled", "cancelei", "emiti", "enviei") or the customer's
# own subject ("your order #N has been cancelled", "seu pedido #N foi cancelado", "your refund").
# Rules and conditions never match:
# - every pattern starts with `_EN_CLAUSE` / `_PT_CLAUSE`: no conditional, causal, temporal or
#   habitual word ("if", "since", "because", "once", "typically", "se", "como", "já que",
#   "depois que", "normalmente"...) may appear EARLIER in the same clause, so the guard covers
#   the whole subject ("If your order #1001 was cancelled" is a rule, whatever the subject is);
# - a condition attached after the claim ("is cancelled if payment fails") also cancels it;
# - the first person may not follow a relative or a negation ("orders that we cancelled",
#   "pedidos que cancelamos", "não cancelei").
# `check_refund_policy` is a read-only policy lookup: reading the policy is never a promise.

_EN_COND = (
    r"if|when|whenever|once|unless|after|before|until|till|whether|since|because|"
    r"as\s+soon\s+as|as\s+long\s+as|so\s+long\s+as|provided|in\s+case|in\s+most\s+cases|"
    r"typically|normally|usually|generally|often|sometimes|always"
)
_PT_COND = (
    r"se|quando|caso|como|ja\s+que|depois\s+que|depois\s+de|apos|antes\s+que|antes\s+de|"
    r"assim\s+que|logo\s+que|desde\s+que|uma\s+vez\s+que|enquanto|sempre\s+que|ate\s+que|"
    r"a\s+menos\s+que|porque|normalmente|geralmente|em\s+geral|costuma|costumam|tipicamente|"
    r"habitualmente"
)
_EN_TRAIL = r"only\s+if|if|unless|provided|as\s+long\s+as|when"
_PT_TRAIL = r"somente\s+se|so\s+se|apenas\s+se|se|caso|a\s+menos\s+que|desde\s+que|quando"
_EN_CLAUSE = _clause_prefix_without(_EN_COND)
_PT_CLAUSE = _clause_prefix_without(_PT_COND)
_EN_END = _not_followed_in_clause_by(_EN_TRAIL)
_PT_END = _not_followed_in_clause_by(_PT_TRAIL)
_EN_REL = r"(?<!\bthat\s)(?<!\bwhich\s)(?<!\bwho\s)(?<!\borders\s)(?<!\bitems\s)(?<!\bnot\s)"
_PT_REL = r"(?<!\bque\s)(?<!\bnao\s)(?<!\bnunca\s)(?<!\bpedidos\s)(?<!\bnao\sja\s)"

_AMOUNT = r"(?:(?:USD|US\$|R\$|\$)\s?\d[\d.,]*|\d[\d.,]*\s?(?:USD|dollars?|reais))"
_EN_I = rf"{_EN_REL}\b(?:I|we)(?:'ve|\s+have|\s+just|\s+already)?\s+"
# A verb coordinated with an earlier first-person claim ("I cancelled ... and issued your refund").
_EN_AND = r"\band\s+(?:also\s+)?(?:have\s+)?"
_EN_ADV = (
    r"(?:just\s+|already\s+|now\s+|successfully\s+|also\s+|gone\s+ahead\s+and\s+|"
    r"went\s+ahead\s+and\s+)*"
)
_EN_DONE = (
    r"(?:has\s+(?:now\s+|already\s+)?been|have\s+(?:now\s+|already\s+)?been|was|were|"
    r"is\s+now|got)\s+(?:successfully\s+|already\s+|now\s+)?"
)
_EN_OWN_ORDER = r"(?:your\s+(?:\w+\s+){0,2}order(?:\s+#?\d+)?|(?:the\s+)?order\s+#?\d{2,})"
# Plural subjects: "your orders (#1001 and #1002)", "orders #1001 and #1002".
_EN_NUMS = r"#?\d+(?:\s*,\s*#?\d+)*(?:\s*,?\s+(?:and|&)\s+#?\d+)?"
_EN_OWN_ORDERS = rf"(?:your\s+(?:\w+\s+)?orders(?:\s+{_EN_NUMS})?|(?:the\s+)?orders\s+{_EN_NUMS})"
_EN_DONE_PLURAL = (
    r"(?:have\s+(?:now\s+|already\s+|all\s+|both\s+)?been|were|are\s+now|are)\s+"
    r"(?:successfully\s+|already\s+|now\s+|all\s+|both\s+)?"
)
_EN_SUBSCRIPTION = r"(?:subscription|plan|membership)"
# The first person cancelling a subscription is `cancel_subscription`, not `cancel_order`.
_EN_NOT_SUBSCRIPTION = rf"(?!\s+(?:your|the|my)\s+(?:\w+\s+)?{_EN_SUBSCRIPTION}\b)"
_EN_OWN_REFUND = (
    r"(?:your\s+(?:full\s+|partial\s+)?refund|(?:the\s+)?refund\s+(?:for|on)\s+"
    rf"(?:your\s+)?order\s+#?\d+|an?\s+(?:full\s+|partial\s+)?refund\s+of\s+{_AMOUNT})"
    rf"(?:\s+(?:of|for)\s+{_AMOUNT})?"
)
_EN_CHECK = (
    rf"{_EN_I}{_EN_ADV}(?:checked|looked\s+up|looked\s+at|reviewed|pulled\s+up|"
    r"retrieved|verified|found|listed|consulted|fetched)\s+"
)

_PT_I = rf"{_PT_REL}\b(?:ja\s+)?"
_PT_DONE = (
    r"(?:ja\s+)?(?:foi|foram|esta|estao|ficou|acabou\s+de\s+ser)\s+"
    r"(?:devidamente\s+|com\s+sucesso\s+|ja\s+)?"
)
_PT_OWN_ORDER = (
    r"(?:(?:[oa]\s+)?(?:seu|sua)\s+(?:\w+\s+)?(?:pedido|encomenda|compra)(?:\s+#?\d+)?|"
    r"(?:[oa]\s+)?(?:pedido|encomenda|compra)\s+#?\d{2,})"
)
_PT_NUMS = r"#?\d+(?:\s*,\s*#?\d+)*(?:\s*,?\s+(?:e|&)\s+#?\d+)?"
_PT_OWN_ORDERS = (
    rf"(?:(?:[oa]s\s+)?(?:seus|suas)\s+(?:\w+\s+)?(?:pedidos|encomendas|compras)(?:\s+{_PT_NUMS})?|"
    rf"(?:[oa]s\s+)?(?:pedidos|encomendas|compras)\s+{_PT_NUMS})"
)
_PT_DONE_PLURAL = (
    r"(?:ja\s+)?(?:foram|estao|ficaram|acabaram\s+de\s+ser)\s+"
    r"(?:devidamente\s+|com\s+sucesso\s+|ja\s+|tod[oa]s\s+)?"
)
_PT_SUBSCRIPTION = r"(?:(?:a\s+)?sua\s+(?:\w+\s+)?assinatura|(?:o\s+)?seu\s+(?:\w+\s+)?plano)"
_PT_NOT_SUBSCRIPTION = r"(?!\s+(?:a\s+|o\s+)?(?:sua|seu)\s+(?:\w+\s+)?(?:assinatura|plano)\b)"
_PT_YOU = r"(?:-lhe\s+|\s+lhe\s+|\s+para\s+voce\s+|\s+a\s+voce\s+|\s+pra\s+voce\s+|\s+)"
_PT_CHECK = (
    rf"{_PT_I}(?:verifiquei|consultei|chequei|conferi|olhei|busquei|levantei|"
    r"analisei|revisei|verificamos|consultamos|conferimos|analisamos)\s+"
)
_START = r"(?:^\s*|(?<=[.;:!?]\s)|(?<=\n))(?:(?:done|ok|okay|great|pronto|feito|certo)[!,.]?\s+)?"


def _en(core: str) -> re.Pattern[str]:
    """Clause-guarded English promise; the claim itself is the `core` group."""
    return _compile(f"{_EN_CLAUSE}(?P<core>{core}){_EN_END}")


def _pt(core: str) -> re.Pattern[str]:
    """Clause-guarded Portuguese promise; the claim itself is the `core` group."""
    return _compile(f"{_PT_CLAUSE}(?P<core>{core}){_PT_END}")


ACTION_PROMISE_PATTERNS: dict[str, dict[Lang, tuple[re.Pattern[str], ...]]] = {
    "list_orders": {
        "en": (
            _en(
                rf"{_EN_CHECK}(?:all\s+(?:of\s+)?)?your\s+(?:\w+\s+)?"
                r"(?:orders|order\s+history|purchases)\b"
            ),
        ),
        "pt": (
            _pt(
                rf"{_PT_CHECK}(?:tod[oa]s\s+)?(?:[oa]s\s+)?(?:seus|suas)\s+(?:\w+\s+)?"
                r"(?:pedidos|compras|encomendas)\b"
                rf"|(?:{_PT_CHECK})(?:o\s+)?seu\s+historico\s+de\s+pedidos\b"
            ),
        ),
    },
    "get_order": {
        "en": (_en(rf"{_EN_CHECK}(?:your|the)\s+order\b(?!s)(?:\s+#?\d+)?"),),
        "pt": (_pt(rf"{_PT_CHECK}(?:o\s+)?(?:seu\s+)?(?:pedido|encomenda)\b(?!s)(?:\s+#?\d+)?"),),
    },
    "cancel_order": {
        "en": (
            _en(rf"{_EN_I}{_EN_ADV}cancell?ed\b{_EN_NOT_SUBSCRIPTION}"),
            _en(rf"{_EN_AND}cancell?ed\s+(?:your\s+(?:\w+\s+)?order|order\s+#?\d+|it)\b"),
            _en(
                rf"{_EN_I}{_EN_ADV}(?:processed|completed|confirmed|submitted|finali[sz]ed)\s+"
                r"(?:the\s+|your\s+)?cancell?ation\b"
            ),
            _en(rf"\b{_EN_OWN_ORDER}\s+(?:{_EN_DONE}|is\s+)cancell?ed\b"),
            _en(rf"\b{_EN_OWN_ORDERS}\s+{_EN_DONE_PLURAL}cancell?ed\b"),
            _en(
                r"\bthe\s+order\s+(?:has\s+(?:now\s+|already\s+)?been|is\s+now)\s+"
                r"(?:successfully\s+)?cancell?ed\b"
            ),
            _en(
                r"\byour\s+cancell?ation(?:\s+request)?\s+(?:has\s+been|was|is\s+now|is)\s+"
                r"(?:successfully\s+)?(?:confirmed|completed?|processed|done|finali[sz]ed)\b"
            ),
            _compile(
                rf"{_START}(?:your\s+)?order(?:\s+#?\d+)?\s+(?:successfully\s+)?cancell?ed"
                r"(?:\s+successfully)?\s*(?:[.!]|$)"
            ),
        ),
        "pt": (
            _pt(rf"{_PT_I}(?:cancelei|cancelamos)\b{_PT_NOT_SUBSCRIPTION}"),
            _pt(
                rf"{_PT_I}(?:processei|conclui|confirmei|efetuei|realizei|fiz|processamos|"
                r"concluimos|confirmamos|efetuamos|realizamos|fizemos)\s+(?:o\s+)?(?:seu\s+)?"
                r"cancelamento\b"
            ),
            _pt(rf"\b{_PT_OWN_ORDER}\s+{_PT_DONE}cancelad[oa]\b"),
            _pt(rf"\b{_PT_OWN_ORDERS}\s+{_PT_DONE_PLURAL}cancelad[oa]s\b"),
            _pt(
                r"\b(?:o\s+)?(?:seu\s+)?cancelamento\s+"
                r"(?:do\s+(?:seu\s+)?pedido(?:\s+#?\d+)?\s+)?(?:ja\s+)?(?:foi|esta)\s+"
                r"(?:concluido|confirmado|realizado|efetuado|feito|processado|finalizado)\b"
            ),
            _compile(
                rf"{_START}(?:seu\s+)?(?:pedido|encomenda)(?:\s+#?\d+)?\s+cancelad[oa]"
                r"(?:\s+com\s+sucesso)?\s*(?:[.!]|$)"
            ),
        ),
    },
    "get_subscription": {
        "en": (_en(rf"{_EN_CHECK}your\s+(?:\w+\s+)?(?:subscription|plan)\b"),),
        "pt": (_pt(rf"{_PT_CHECK}(?:a\s+|o\s+)?(?:sua|seu)\s+(?:assinatura|plano)\b"),),
    },
    "check_refund_policy": {"en": (), "pt": ()},
    "issue_refund": {
        "en": (
            _en(
                rf"(?:{_EN_I}{_EN_ADV}|{_EN_AND})(?:issued|processed|initiated|sent|approved|granted|"
                r"submitted|completed|applied)\s+(?:you\s+)?(?:a\s+|the\s+|your\s+)?"
                rf"(?:{_AMOUNT}\s+)?(?:full\s+|partial\s+)?refund\b"
            ),
            _en(
                rf"{_EN_I}{_EN_ADV}refunded\s+(?:you|your|it|the\s+(?:full\s+)?(?:amount|order|charge)|"
                rf"order\s+#?\d+|{_AMOUNT})"
            ),
            _en(
                rf"\b{_EN_OWN_REFUND}\s+{_EN_DONE}(?:issued|processed|approved|sent|initiated|"
                r"completed|granted|credited)\b"
            ),
            _en(
                r"\bthe\s+refund\s+(?:has\s+(?:now\s+|already\s+)?been|is\s+now)\s+"
                r"(?:successfully\s+)?(?:issued|processed|approved|sent|completed|credited)\b"
            ),
            _en(r"\byour\s+(?:full\s+|partial\s+)?refund\s+is\s+on\s+(?:its|the)\s+way\b"),
            _compile(
                rf"{_START}refund\s+(?:successfully\s+)?(?:issued|processed|completed)"
                r"\s+(?:for|on)\s+(?:your\s+)?order\b"
            ),
            _en(rf"{_EN_REL}\byou\s+{_EN_DONE}refunded\b"),
            _en(rf"\b{_EN_OWN_ORDER}\s+{_EN_DONE}refunded\b"),
            _en(rf"\b{_EN_OWN_ORDERS}\s+{_EN_DONE_PLURAL}refunded\b"),
            _en(
                rf"\b(?:your\s+refunds|the\s+refunds\s+(?:for|on)\s+(?:your\s+)?orders\s+{_EN_NUMS})"
                rf"(?:\s+(?:for|on)\s+(?:your\s+)?orders\s+{_EN_NUMS})?\s+{_EN_DONE_PLURAL}"
                r"(?:issued|processed|approved|sent|completed|credited)\b"
            ),
        ),
        "pt": (
            _pt(
                rf"{_PT_I}(?:emiti|processei|aprovei|realizei|efetuei|fiz|solicitei|"
                r"emitimos|processamos|aprovamos|realizamos|efetuamos|fizemos)\s+"
                r"(?:o\s+|um\s+|seu\s+|o\s+seu\s+)?reembolso\b"
            ),
            _pt(rf"{_PT_I}(?:reembolsei|reembolsamos)\b"),
            _compile(
                rf"{_START}reembolso(?:\s+(?:de|total|parcial)\s+\S+(?:\s+\S+)?)?\s+"
                r"(?:emitido|processado|realizado|efetuado|concluido)\s+(?:para|do)\s+"
                r"(?:o\s+)?(?:seu\s+)?pedido\b"
            ),
            _pt(
                r"\b(?:(?:o\s+)?seu\s+reembolso|o\s+reembolso(?:\s+(?:do|para\s+o)\s+"
                r"(?:seu\s+)?pedido\s+#?\d+)?)(?:\s+(?:de|total|parcial)\s+\S+(?:\s+\S+)?)?\s+"
                rf"{_PT_DONE}(?:emitido|processado|aprovado|realizado|efetuado|concluido|"
                r"creditado|feito|liberado)\b"
            ),
            _pt(rf"{_PT_REL}\bvoce\s+{_PT_DONE}reembolsad[oa]\b"),
            _pt(rf"\b{_PT_OWN_ORDER}\s+{_PT_DONE}reembolsad[oa]\b"),
            _pt(rf"\b{_PT_OWN_ORDERS}\s+{_PT_DONE_PLURAL}reembolsad[oa]s\b"),
            _pt(
                r"\b(?:os\s+)?(?:seus\s+reembolsos|reembolsos\s+(?:dos|para\s+os)\s+(?:seus\s+)?"
                rf"pedidos\s+{_PT_NUMS})\s+{_PT_DONE_PLURAL}(?:emitidos|processados|aprovados|"
                r"realizados|efetuados|concluidos|creditados|feitos|liberados)\b"
            ),
        ),
    },
    "send_email": {
        "en": (
            _en(
                rf"{_EN_I}{_EN_ADV}(?:sent|emailed|e-mailed|forwarded|mailed)\s+"
                r"(?:you\s+|it\s+to\s+you\s+)?(?:an?\s+|the\s+|your\s+)?"
                r"(?:confirmation\s+|summary\s+)?(?:e-?mail|message|copy|confirmation|summary)\b"
            ),
            _en(rf"{_EN_I}{_EN_ADV}(?:emailed|e-mailed)\s+you\b"),
            _en(
                r"\b(?:an?|the|your)\s+(?:confirmation\s+|summary\s+)?e-?mail\s+"
                rf"(?:\w+\s+)?{_EN_DONE}(?:sent|delivered|forwarded)\b"
            ),
            _compile(
                rf"{_START}(?:the\s+|a\s+|your\s+)?(?:confirmation|summary)\s+e-?mail\s+"
                r"(?:successfully\s+)?sent\b"
            ),
        ),
        "pt": (
            _pt(
                rf"{_PT_I}(?:enviei|mandei|encaminhei|enviamos|mandamos|encaminhamos)"
                rf"{_PT_YOU}(?:um\s+|uma\s+|o\s+|a\s+)?(?:e-?mail|mensagem|confirmacao|resumo)\b"
            ),
            _pt(
                r"\b(?:o|um|seu)\s+(?:e-?mail|mensagem)\s+(?:de\s+\w+\s+)?"
                rf"{_PT_DONE}(?:enviad[oa]|encaminhad[oa]|disparad[oa])\b"
            ),
            _pt(
                rf"{_PT_REL}\b(?:ja\s+)?(?:foi|acabou\s+de\s+ser)\s+(?:enviad[oa]|"
                r"encaminhad[oa])\s+(?:um|uma|o|a)\s+(?:e-?mail|mensagem)\b"
            ),
        ),
    },
}

# Promises of an action no tool can perform. The output guard never finds them in the audit, so
# they always block: there is no tool that cancels a subscription (`cancel_subscription`).
CANCEL_SUBSCRIPTION = "cancel_subscription"
NON_TOOL_PROMISE_PATTERNS: dict[str, dict[Lang, tuple[re.Pattern[str], ...]]] = {
    CANCEL_SUBSCRIPTION: {
        "en": (
            _en(rf"{_EN_I}{_EN_ADV}cancell?ed\s+(?:your|the)\s+(?:\w+\s+)?{_EN_SUBSCRIPTION}\b"),
            _en(
                rf"\byour\s+(?:\w+\s+)?{_EN_SUBSCRIPTION}\s+(?:{_EN_DONE}|is\s+now\s+)"
                r"cancell?ed\b"
            ),
        ),
        "pt": (
            _pt(rf"{_PT_I}(?:cancelei|cancelamos)\s+{_PT_SUBSCRIPTION}\b"),
            _pt(rf"\b{_PT_SUBSCRIPTION}\s+{_PT_DONE}cancelad[oa]\b"),
        ),
    },
}

_ITEM = r"(?:(?:orders?|pedidos?|encomendas?|compras?)\s+(?:#\s*)?(?:n[º°o.]\s*)?|#\s*)(\d+)\b"
_ORDER_ID = re.compile(_ITEM, re.IGNORECASE)
# The object list right after a first-person verb: "order #1001 and #1002", "for order #1004",
# "your order #1001 and your order #1002", "os pedidos #1001 e #1002".
_DET = r"(?:(?:your|the|my|o|a|os|as|seu|sua|seus|suas)\s+)?"
_FIRST_ITEM = re.compile(
    rf"\s*(?:(?:for|on|of|to|from|para|do|da|de|no|na|dos|das)\s+)?{_DET}(?:\w+\s+)?{_ITEM}",
    re.IGNORECASE,
)
_NEXT_ITEM = re.compile(
    rf"\s*(?:,\s*(?:and\s+|e\s+)?|\s+(?:and|e|&)\s+){_DET}(?:\w+\s+)?{_ITEM}",
    re.IGNORECASE,
)


def _cited_orders(text: str, start: int, end: int) -> list[int | None]:
    """Orders cited in the promise `text[start:end]`: every id inside it; when there is none,
    the list of orders that is the object of the verb right after it. `[None]` if none."""
    inside = [int(m.group(1)) for m in _ORDER_ID.finditer(text, start, end)]
    if inside:
        return list(dict.fromkeys(inside))
    ids: list[int] = []
    item = _FIRST_ITEM.match(text, end)
    while item is not None:
        ids.append(int(item.group(1)))
        item = _NEXT_ITEM.match(text, item.end())
    return list(dict.fromkeys(ids)) or [None]


def _promise_spans(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[tuple[int, int]]:
    """Spans of every match, overlapping matches merged into one promise."""
    spans = sorted(
        m.span("core") if "core" in p.groupindex else m.span()
        for p in patterns
        for m in p.finditer(text)
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def find_action_promises(sentence: str) -> list[tuple[str, int | None]]:
    """(tool, cited order id or None) for each action `sentence` claims is done.

    Additive helper for the output guard, built on `ACTION_PROMISE_PATTERNS` (both languages)
    plus `NON_TOOL_PROMISE_PATTERNS` (`cancel_subscription`, which no tool performs, so it is
    never backed). Every distinct promise counts (compared by span, not by tool): one entry per
    cited order ("I've cancelled order #1001 and #1002" -> two entries) and `None` for a promise
    that cites no order, even when another promise of the same tool does. Ordered by tool, then
    by position; exact duplicates removed.
    """
    text = _normalize(sentence)
    found: list[tuple[str, int | None]] = []
    for tool, by_lang in {**ACTION_PROMISE_PATTERNS, **NON_TOOL_PROMISE_PATTERNS}.items():
        for start, end in _promise_spans(text, by_lang["en"] + by_lang["pt"]):
            for order_id in _cited_orders(text, start, end):
                if (tool, order_id) not in found:
                    found.append((tool, order_id))
    return found
