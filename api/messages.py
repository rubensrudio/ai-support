"""Fixed texts shown to the Customer, in Portuguese and English (CT-4, spec section 9).

Every key has a non-empty text in both languages. Keys whose value matches an `ErrorCode`
hold the user message of that error. LLM-generated text is never translated here.
"""

from enum import StrEnum
from typing import Literal

Lang = Literal["pt", "en"]


class MessageKey(StrEnum):
    # Chat outcomes (spec section 9)
    OUT_OF_CORPUS = "OUT_OF_CORPUS"
    KNOWLEDGE_UNAVAILABLE = "KNOWLEDGE_UNAVAILABLE"
    CONFIRM_ACTION = "CONFIRM_ACTION"
    CONFIRM_BUTTON = "CONFIRM_BUTTON"
    REFUSE_BUTTON = "REFUSE_BUTTON"
    DEGRADED_RESPONSE = "DEGRADED_RESPONSE"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    AGENT_LIMIT_REACHED = "AGENT_LIMIT_REACHED"
    MESSAGE_EMPTY = "MESSAGE_EMPTY"

    # Agent step labels, one per tool (STR-02)
    STEP_LIST_ORDERS = "STEP_LIST_ORDERS"
    STEP_GET_ORDER = "STEP_GET_ORDER"
    STEP_CANCEL_ORDER = "STEP_CANCEL_ORDER"
    STEP_CHECK_REFUND_POLICY = "STEP_CHECK_REFUND_POLICY"
    STEP_ISSUE_REFUND = "STEP_ISSUE_REFUND"
    STEP_GET_SUBSCRIPTION = "STEP_GET_SUBSCRIPTION"
    STEP_SEND_EMAIL = "STEP_SEND_EMAIL"
    STEP_GENERIC = "STEP_GENERIC"

    # One per ErrorCode (plan 8.3)
    VALIDATION_ERROR = "VALIDATION_ERROR"
    MESSAGE_TOO_LONG = "MESSAGE_TOO_LONG"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
    CONFIRMATION_NOT_FOUND = "CONFIRMATION_NOT_FOUND"
    CONFIRMATION_NOT_PENDING = "CONFIRMATION_NOT_PENDING"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    DAILY_CAP_REACHED = "DAILY_CAP_REACHED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    TRIAGE_UNAVAILABLE = "TRIAGE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    STREAM_ERROR = "STREAM_ERROR"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"


_UNSUPPORTED_LANGUAGE_EN = (
    "Sorry, I can only help in Portuguese or English. "
    "Please write your message in one of these languages."
)

_TEXTS: dict[MessageKey, dict[Lang, str]] = {
    MessageKey.OUT_OF_CORPUS: {
        "pt": "Não encontrei essa informação na base de conhecimento da Assinaflow.",
        "en": "I couldn't find this information in the Assinaflow knowledge base.",
    },
    MessageKey.KNOWLEDGE_UNAVAILABLE: {
        "pt": "Não consigo consultar a base de conhecimento agora. Tente de novo em instantes.",
        "en": "I can't access the knowledge base right now. Please try again in a moment.",
    },
    MessageKey.CONFIRM_ACTION: {
        "pt": "Confirmar: {action} {target}?",
        "en": "Confirm: {action} {target}?",
    },
    MessageKey.CONFIRM_BUTTON: {"pt": "Confirmar", "en": "Confirm"},
    MessageKey.REFUSE_BUTTON: {"pt": "Recusar", "en": "Decline"},
    MessageKey.DEGRADED_RESPONSE: {
        "pt": (
            "Não consegui responder agora. Tente em instantes. "
            "Abrimos um ticket e nossa equipe vai acompanhar o seu caso."
        ),
        "en": (
            "I couldn't answer right now. Please try again in a moment. "
            "We opened a ticket and our team will follow up on your case."
        ),
    },
    MessageKey.GUARDRAIL_BLOCKED: {
        "pt": "Desculpe, não posso ajudar com esse pedido.",
        "en": "Sorry, I can't help with that request.",
    },
    MessageKey.UNSUPPORTED_LANGUAGE: {
        # CHAT-95: always in English, whatever the conversation language.
        "pt": _UNSUPPORTED_LANGUAGE_EN,
        "en": _UNSUPPORTED_LANGUAGE_EN,
    },
    MessageKey.AGENT_LIMIT_REACHED: {
        "pt": "Não consegui concluir o seu pedido. Ações já executadas: {actions}.",
        "en": "I couldn't complete your request. Actions already taken: {actions}.",
    },
    MessageKey.MESSAGE_EMPTY: {
        "pt": "A mensagem está vazia.",
        "en": "The message is empty.",
    },
    MessageKey.STEP_LIST_ORDERS: {"pt": "listando pedidos…", "en": "listing orders…"},
    MessageKey.STEP_GET_ORDER: {"pt": "consultando pedido…", "en": "looking up order…"},
    MessageKey.STEP_CANCEL_ORDER: {"pt": "cancelando pedido…", "en": "cancelling order…"},
    MessageKey.STEP_CHECK_REFUND_POLICY: {
        "pt": "verificando política de reembolso…",
        "en": "checking refund policy…",
    },
    MessageKey.STEP_ISSUE_REFUND: {"pt": "emitindo reembolso…", "en": "issuing refund…"},
    MessageKey.STEP_GET_SUBSCRIPTION: {
        "pt": "consultando assinatura…",
        "en": "looking up subscription…",
    },
    MessageKey.STEP_SEND_EMAIL: {"pt": "enviando e-mail…", "en": "sending email…"},
    MessageKey.STEP_GENERIC: {"pt": "processando…", "en": "working on it…"},
    MessageKey.VALIDATION_ERROR: {
        "pt": "A mensagem está vazia ou algum campo é inválido.",
        "en": "The message is empty or a field is invalid.",
    },
    MessageKey.MESSAGE_TOO_LONG: {
        "pt": "A mensagem passou de {max_chars} caracteres.",
        "en": "The message is longer than {max_chars} characters.",
    },
    MessageKey.AUTH_REQUIRED: {
        "pt": "Entre com uma conta de teste.",
        "en": "Please sign in with a test account.",
    },
    MessageKey.INVALID_CREDENTIALS: {
        "pt": "Usuário ou senha inválidos.",
        "en": "Invalid username or password.",
    },
    MessageKey.CONVERSATION_NOT_FOUND: {
        "pt": "Conversa não encontrada.",
        "en": "Conversation not found.",
    },
    MessageKey.CONFIRMATION_NOT_FOUND: {
        "pt": "Ação não encontrada.",
        "en": "Action not found.",
    },
    MessageKey.CONFIRMATION_NOT_PENDING: {
        "pt": "Essa ação não está mais aguardando confirmação.",
        "en": "This action is no longer awaiting confirmation.",
    },
    MessageKey.MESSAGE_NOT_FOUND: {
        "pt": "Mensagem não encontrada.",
        "en": "Message not found.",
    },
    MessageKey.RATE_LIMITED: {
        "pt": "Você atingiu o limite de mensagens. Tente de novo às {retry_at}.",
        "en": "You have reached the message limit. Please try again at {retry_at}.",
    },
    MessageKey.DAILY_CAP_REACHED: {
        "pt": "A demo atingiu o limite diário e volta às 00:00 UTC.",
        "en": "The demo has reached its daily limit and will be back at 00:00 UTC.",
    },
    MessageKey.LLM_UNAVAILABLE: {
        "pt": "Não consegui responder agora. Tente de novo.",
        "en": "I couldn't answer right now. Please try again.",
    },
    MessageKey.TRIAGE_UNAVAILABLE: {
        "pt": "Triagem indisponível no momento.",
        "en": "Triage is unavailable right now.",
    },
    MessageKey.INTERNAL_ERROR: {
        "pt": "Erro inesperado. Tente de novo.",
        "en": "Unexpected error. Please try again.",
    },
    MessageKey.STREAM_ERROR: {
        "pt": "Houve um erro. Tente de novo.",
        "en": "Something went wrong. Please try again.",
    },
    MessageKey.NOT_FOUND: {
        "pt": "Recurso não encontrado.",
        "en": "Resource not found.",
    },
    MessageKey.METHOD_NOT_ALLOWED: {
        "pt": "Método não permitido.",
        "en": "Method not allowed.",
    },
}


def message_template(key: MessageKey, lang: Lang) -> str:
    """Raw text of `key` in `lang`, with its `{param}` placeholders not filled in."""
    return _TEXTS[key][lang]


def user_message(key: MessageKey, lang: Lang, **params: object) -> str:
    """Text of `key` in `lang` with `params` interpolated.

    Raises `KeyError` when the text needs a parameter that was not given. Extra
    parameters are ignored.
    """
    return message_template(key, lang).format(**params)
