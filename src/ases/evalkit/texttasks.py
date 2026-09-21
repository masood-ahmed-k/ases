"""The evaluation tasks whose answer is text and whose scorer is a keyword or structure check: E1 Requirements, E2
Architecture, E3 Repository understanding, E7 Security, E9 Tool use and E10 Review (blueprint Appendix D.1).

Every scorer here is a documented HEURISTIC over free text (evalkit/text.py): it can be gamed by an answer that
stuffs keywords and can miss a right answer worded in an unexpected way. What keeps them honest is that each has a
threshold in a named constant, a precision guard where a shotgun answer would otherwise win (E7), and tests that pin
what it accepts and what it rejects. None of them calls a model.
"""
from __future__ import annotations

import difflib
import pathlib
import re

from . import codeeval, text
from .model import KIND_REPO, KIND_TEXT, EvalTask, InvokeResult, Score

_PREAMBLE = (
    "This is an automated evaluation of your written answer, not a conversation: nobody will reply to a question, "
    "so answer directly and completely.\n\n"
)


# ---- E1 Requirements: vague requirements into explicit assumptions --------------------------------------------

E1_REQUIREMENT = (
    "We run a small chain of bakeries. We want customers to order cakes online and collect them in the shop. It has "
    "to be fast and secure, work well on phones, and let the shops see what is coming up. People forget their "
    "orders, so reminders would help. Keep it simple, but it needs to be able to grow with us."
)

# The ambiguities a competent analyst finds in E1_REQUIREMENT, as regex patterns over normalize()d text. A pattern is
# deliberately NOT satisfied by echoing the request back ('phones', 'fast', 'secure', 'grow' are not patterns).
E1_TOPICS: dict[str, tuple[str, ...]] = {
    "users_and_roles": (r"\b(?:staff|employees?|bakers?|managers?|admin\w*|owners?|roles?|permissions?)\b",),
    "order_changes": (
        r"\b(?:cancel\w*|refund\w*|amend\w*|modif\w*|reschedul\w*|lead time|cut ?off|"
        r"(?:change|edit) (?:an|their|the) order)\b",
    ),
    "payment": (
        r"\b(?:payments?|pay|paid|prepay\w*|deposits?|checkout|credit card|debit card|invoice\w*|stripe|paypal)\b",
    ),
    "reminders": (
        r"\b(?:e ?mail|sms|text messages?|push notifications?|whatsapp)\b",
        r"\bremind\w*\b.{0,80}\b(?:hours?|days?|minutes?|before|prior|morning)\b",
    ),
    "performance": (
        r"\b(?:latency|response times?|load times?|page loads?|concurrent|throughput|peak (?:load|traffic|hours?|times?)"
        r"|milliseconds?|ms|seconds?)\b",
    ),
    "security_privacy": (
        r"\b(?:authenticat\w*|passwords?|log ?in|sign ?in|encrypt\w*|tls|https|ssl|gdpr|personal data|privacy|pci|"
        r"two factor|2fa|hash\w*)\b",
    ),
    "devices": (
        r"\b(?:responsive|mobile web|native apps?|mobile apps?|progressive web|pwa|ios|android|browsers?|"
        r"screen sizes?|smartphones?|touch)\b",
    ),
    "shop_view": (
        r"\b(?:dashboard|upcoming orders?|calendar|daily (?:list|view|summary)|production (?:list|schedule|plan)|"
        r"pick ?up (?:times?|slots?|windows?)|order queue|schedule)\b",
    ),
    "growth": (
        r"\b(?:scal\w*|more shops|additional shops|new shops|multi ?(?:tenant|shop|store|location)|"
        r"number of (?:shops|stores|orders|locations)|\d+ (?:shops|stores|locations|orders)|growth|expan\w*)\b",
    ),
}
E1_MIN_TOPICS = 7   # of the 9 above
E1_MIN_ITEMS = 6    # list items: 'a list of explicit assumptions' is a list


def _e1_fixture(workdir: pathlib.Path) -> dict:
    return {"requirement": E1_REQUIREMENT}


def _e1_prompt(fixture: dict) -> str:
    return (
        _PREAMBLE
        + "You are the requirements analyst on a software project. The client sent this request:\n\n"
        + fixture["requirement"]
        + "\n\nThe request is vague on purpose. Do not build anything and do not ask the client questions. Write "
        "down what you would assume instead:\n"
        "1. A numbered list of explicit assumptions, one per line. Each is a concrete decision (who, what, how "
        "many, how fast, which channel), stated as something you are assuming, not as a question.\n"
        "2. Then a short list of the open questions you would still confirm with the client.\n"
        "Cover every ambiguity you can find."
    )


def _e1_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    hits = text.topic_hits(output, E1_TOPICS)
    covered = sum(hits.values())
    items = text.list_item_count(output)
    states_assumptions = "assum" in text.normalize(output)
    missing = ",".join(name for name, hit in hits.items() if not hit)
    success = covered >= E1_MIN_TOPICS and items >= E1_MIN_ITEMS and states_assumptions
    return Score(
        success=success,
        findings={
            "topics_covered": covered, "topics_total": len(E1_TOPICS), "topics_needed": E1_MIN_TOPICS,
            "list_items": items, "items_needed": E1_MIN_ITEMS, "states_assumptions": states_assumptions,
            "missing_topics": missing,
        },
        notes="keyword heuristic over normalised text",
    )


# ---- E2 Architecture: clean boundaries and interfaces ---------------------------------------------------------

E2_SPEC = (
    "A notification service for an online shop. Other services send it events (order shipped, password reset "
    "requested). It renders a message from a template and delivers it by email or SMS according to the user's "
    "preference. Failed deliveries are retried with backoff, and a message that keeps failing is moved to a "
    "dead-letter store for later inspection. Every delivery attempt is recorded in an audit log. Operators can query "
    "the delivery status of a message. The same event received twice must not produce two messages."
)

E2_COMPONENTS: dict[str, tuple[str, ...]] = {
    "event_intake": (
        r"\b(?:intake|ingest\w*|event (?:api|endpoint|receiver|gateway|listener)|http (?:api|endpoint)|rest api|"
        r"api gateway|receiv\w+ events?|post events?)\b",
    ),
    "queue": (r"\b(?:queue|broker|kafka|rabbitmq|sqs|message bus|event bus)\b",),
    "template_renderer": (r"\b(?:templat\w+|renderer|render)\b",),
    "channel_adapters": (
        r"\b(?:email|sms)\b (?:\w+ ){0,2}(?:adapters?|providers?|senders?|channels?|gateways?|drivers?|connectors?)\b",
        r"\b(?:adapters?|channels?|providers?|senders?) (?:\w+ ){0,2}(?:email|sms)\b",
        r"\bdelivery (?:workers?|adapters?|service|channels?)\b",
    ),
    "retry_scheduler": (r"\b(?:retr(?:y|ies|ied|ying)|backoff)\b",),
    "dead_letter_store": (r"\b(?:dead ?letter|dlq)\b",),
    "audit_log": (r"\b(?:audit|delivery log|attempt log)\b",),
    "status_query": (
        r"\b(?:status (?:api|endpoint|query|lookup|service|check)|get messages?|delivery status|query (?:the )?status)\b",
    ),
    "deduplication": (r"\b(?:idempoten\w*|dedup\w*|de dup\w*|duplicates?|exactly once|at most once)\b",),
}
E2_MIN_COMPONENTS = 7  # of the 9 above
E2_MIN_INTERFACE_LINES = 6

# A line that states an interface: an HTTP verb and path, a call with parentheses, or an arrow.
_INTERFACE_LINE = re.compile(
    r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/\S*|\b[A-Za-z_][A-Za-z0-9_]*\([^)\n]*\)|->|=>"
)


def _e2_fixture(workdir: pathlib.Path) -> dict:
    return {"spec": E2_SPEC}


def _e2_prompt(fixture: dict) -> str:
    return (
        _PREAMBLE
        + "You are the architect. Design the system below. Do not write implementation code.\n\n"
        + fixture["spec"]
        + "\n\nReply with:\n"
        "1. The components: a name and one line of purpose for each.\n"
        "2. For every component its interface: the calls, endpoints or messages it exposes and what goes in and out, "
        "for example `POST /events -> 202 {message_id}` or `render(template_id, context) -> text`.\n"
        "3. The path of one message from arrival to delivery."
    )


def _e2_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    hits = text.topic_hits(output, E2_COMPONENTS)
    covered = sum(hits.values())
    interface_lines = sum(1 for line in output.splitlines() if _INTERFACE_LINE.search(line))
    missing = ",".join(name for name, hit in hits.items() if not hit)
    success = covered >= E2_MIN_COMPONENTS and interface_lines >= E2_MIN_INTERFACE_LINES
    return Score(
        success=success,
        findings={
            "components_named": covered, "components_total": len(E2_COMPONENTS),
            "components_needed": E2_MIN_COMPONENTS, "interface_lines": interface_lines,
            "interface_lines_needed": E2_MIN_INTERFACE_LINES, "missing_components": missing,
        },
        notes="keyword heuristic over normalised text",
    )


# ---- E3 Repository understanding: a generated repository plus questions with known answers -------------------

E3_FILES: dict[str, str] = {
    "inventory/__init__.py": '"""Tiny stock keeping library used by the shop scripts."""\n\n__version__ = "0.4.2"\n',
    "inventory/config.py": (
        '"""Settings, read when a command starts."""\n'
        "import os\n\n"
        'DB_ENV_VAR = "INVENTORY_DB"\n'
        'DEFAULT_DB_PATH = "stock.sqlite3"\n'
        "MAX_ITEMS_PER_ORDER = 37\n"
        "LOW_STOCK_THRESHOLD = 4\n\n\n"
        "def database_path():\n"
        '    """The database file: the INVENTORY_DB environment variable when it is set, else the default."""\n'
        "    return os.environ.get(DB_ENV_VAR, DEFAULT_DB_PATH)\n"
    ),
    "inventory/models.py": (
        '"""Plain data classes."""\n'
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\n"
        "class Item:\n"
        "    sku: str\n"
        "    name: str\n"
        "    quantity: int\n"
        "    unit_price: float\n\n\n"
        "class LowStockError(Exception):\n"
        '    """Raised when an order asks for more units than are in stock."""\n'
    ),
    "inventory/pricing.py": (
        '"""Price arithmetic."""\n\n'
        "BULK_THRESHOLD = 25\n"
        "BULK_DISCOUNT_RATE = 0.12\n"
        "DEFAULT_TAX_RATE = 0.0725\n\n\n"
        "def bulk_discount(quantity, unit_price):\n"
        '    """Total for one line after the bulk discount, which starts at BULK_THRESHOLD units."""\n'
        "    total = quantity * unit_price\n"
        "    if quantity >= BULK_THRESHOLD:\n"
        "        total *= 1 - BULK_DISCOUNT_RATE\n"
        "    return round(total, 2)\n\n\n"
        "def with_tax(amount, rate=DEFAULT_TAX_RATE):\n"
        "    return round(amount * (1 + rate), 2)\n"
    ),
    "inventory/storage.py": (
        '"""Catalog persistence: one JSON file per catalog."""\n'
        "import json\n\n"
        "from .config import database_path\n"
        "from .models import Item\n\n\n"
        "class CatalogNotFoundError(Exception):\n"
        '    """The catalog file does not exist."""\n\n\n'
        "def load_catalog(path=None):\n"
        "    target = path or database_path()\n"
        "    try:\n"
        '        with open(target, encoding="utf-8") as handle:\n'
        "            rows = json.load(handle)\n"
        "    except FileNotFoundError as exc:\n"
        "        raise CatalogNotFoundError(target) from exc\n"
        "    return [Item(**row) for row in rows]\n\n\n"
        "def save_catalog(items, path=None):\n"
        '    with open(path or database_path(), "w", encoding="utf-8") as handle:\n'
        "        json.dump([vars(item) for item in items], handle)\n"
    ),
    "inventory/cli.py": (
        '"""Command line entry point: python -m inventory.cli <command>."""\n'
        "import argparse\n\n"
        "from . import config, pricing, storage\n\n\n"
        "def build_parser():\n"
        '    parser = argparse.ArgumentParser(prog="inventory")\n'
        '    sub = parser.add_subparsers(dest="command", required=True)\n'
        '    sub.add_parser("report", help="print stock levels and warn about low stock")\n'
        '    restock = sub.add_parser("restock", help="add units to an item")\n'
        '    restock.add_argument("sku")\n'
        '    restock.add_argument("units", type=int)\n'
        "    return parser\n\n\n"
        "def main(argv=None):\n"
        "    args = build_parser().parse_args(argv)\n"
        "    items = storage.load_catalog()\n"
        '    if args.command == "report":\n'
        "        for item in items:\n"
        "            line = pricing.bulk_discount(item.quantity, item.unit_price)\n"
        '            print(f"{item.sku} {item.quantity} {line}")\n'
        "            if item.quantity <= config.LOW_STOCK_THRESHOLD:\n"
        '                print(f"LOW STOCK: {item.sku}")\n'
        '    elif args.command == "restock":\n'
        "        for item in items:\n"
        "            if item.sku == args.sku:\n"
        "                item.quantity += args.units\n"
        "        storage.save_catalog(items)\n"
        "    return 0\n"
    ),
    "README.md": "# inventory\n\nA tiny stock keeping library. See the modules in inventory/.\n",
}


def _ident(name: str) -> str:
    return r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])"


def _number(value: int) -> str:
    """The whole number `value`: not part of a longer number (137, 2500) and not the integer part of a decimal (4.5); a
    full stop that ends a sentence ('the limit is 37.') is fine."""
    return r"(?<![0-9.])" + str(value) + r"(?![0-9]|\.[0-9])"


def _module(name: str) -> str:
    return r"(?<![a-z0-9_])" + re.escape(name) + r"(?:\.py)?(?![a-z0-9_])"


# Each question has the regexes (over the lower-cased text of ITS answer) that must ALL match for it to count as found.
E3_QUESTIONS: tuple[dict, ...] = (
    {"n": 1, "text": "Which environment variable overrides the database path, and in which file is it read?",
     "expect": (_ident("inventory_db"), _module("config"))},
    {"n": 2, "text": "What is the maximum number of items allowed in one order, and in which file is that limit defined?",
     "expect": (_number(37), _module("config"))},
    {"n": 3, "text": "What exception does load_catalog raise when the catalog file does not exist, and in which file is "
                     "that exception class defined?",
     "expect": (_ident("catalognotfounderror"), _module("storage"))},
    {"n": 4, "text": "Which function applies the bulk discount, in which file, and from how many units does the "
                     "discount start?",
     "expect": (_ident("bulk_discount"), _module("pricing"), _number(25))},
    {"n": 5, "text": "Which command line sub-command prints the low stock warning, and at or below what quantity is an "
                     "item treated as low stock?",
     "expect": (_ident("report"), _number(4))},
    {"n": 6, "text": "Which three modules of the inventory package does cli.py import?",
     "expect": (_module("config"), _module("pricing"), _module("storage"))},
)
E3_PASS_FRACTION = 0.8  # 5 of the 6 answers found


def _e3_fixture(workdir: pathlib.Path) -> dict:
    codeeval.write_tree(workdir, E3_FILES)
    return {"root": str(workdir), "questions": [dict(q) for q in E3_QUESTIONS]}


def _e3_prompt(fixture: dict) -> str:
    questions = "\n".join(f"Q{q['n']}: {q['text']}" for q in fixture["questions"])
    return (
        _PREAMBLE
        + "A small Python repository is at this exact absolute path (use it in every tool call and do not rely on any "
        f"working directory): {fixture['root']}\n\n"
        "Use your file tools to read it, then answer the questions below. Every answer must name the file it comes "
        "from. Start each answer on its own line with its label (Q1:, Q2:, and so on) and keep it to one or two "
        "sentences. Do not modify any file.\n\n" + questions
    )


def _e3_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    answers = text.split_numbered_answers(output, "Q")
    questions = fixture["questions"]
    found: list[int] = []
    for question in questions:
        answer = answers.get(question["n"], "")
        if answer and text.matches_all(answer, question["expect"]):
            found.append(question["n"])
    total = len(questions)
    missing = ",".join(f"Q{q['n']}" for q in questions if q["n"] not in found)
    return Score(
        success=total > 0 and len(found) / total >= E3_PASS_FRACTION,
        tests_passed=len(found), tests_total=total,
        findings={"answers_found": len(found), "answers_total": total, "missing_answers": missing,
                  "answers_parsed": len(answers)},
        notes=f"an answer counts when it sits under its Q label and holds every expected fact ({E3_PASS_FRACTION:.0%} needed)",
    )


# ---- E7 Security: a small sample with K seeded vulnerabilities --------------------------------------------------

E7_CODE = '''"""User account service (sample code under review)."""
import hashlib
import os
import pickle
import subprocess

import psycopg2
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

DB_PASSWORD = "Winter2024!prod"
UPLOAD_DIR = "/srv/app/uploads"


def db():
    return psycopg2.connect(host="db.internal", user="app", password=DB_PASSWORD, dbname="users")


@app.route("/user")
def get_user():
    name = request.args["name"]
    cur = db().cursor()
    cur.execute("SELECT id, email FROM users WHERE name = '%s'" % name)
    return jsonify(cur.fetchall())


@app.route("/register", methods=["POST"])
def register():
    password = request.form["password"]
    digest = hashlib.md5(password.encode()).hexdigest()
    cur = db().cursor()
    cur.execute("INSERT INTO users (name, pw) VALUES (%s, %s)", (request.form["name"], digest))
    return "ok"


@app.route("/download")
def download():
    return send_file(os.path.join(UPLOAD_DIR, request.args["file"]))


@app.route("/ping")
def ping():
    host = request.args["host"]
    out = subprocess.run("ping -c 1 " + host, shell=True, capture_output=True, text=True)
    return out.stdout


@app.route("/restore", methods=["POST"])
def restore():
    state = pickle.loads(request.get_data())
    return jsonify(state)


@app.route("/health")
def health():
    return "ok"
'''

# The K = 6 seeded vulnerabilities, as regex patterns over normalize()d text (an answer names one with ANY pattern).
E7_SEEDED: dict[str, tuple[str, ...]] = {
    "sql_injection": (
        r"\bsql injection\b", r"\bsqli\b", r"\bparameteri[sz]ed quer\w*\b", r"\bprepared statements?\b",
        r"\bstring (?:formatting|concatenation|interpolation) (?:in|into|of) (?:the )?(?:sql|quer\w*)\b",
    ),
    "path_traversal": (
        r"\bpath traversal\b", r"\bdirectory traversal\b", r"\bdot dot\b", r"\barbitrary file (?:read|access|download)\b",
        r"\blocal file (?:inclusion|disclosure)\b", r"\bsecure filename\b", r"\bsend from directory\b",
    ),
    "command_injection": (
        r"\bcommand injection\b", r"\bshell injection\b", r"\bos command\b", r"\bshell (?:equals |=)?true\b",
    ),
    "hardcoded_credential": (
        r"\bhard ?coded (?:\w+ ){0,3}(?:password|credential|secret|key|token)s?\b",
        r"\b(?:password|credential|secret)s? (?:is |are )?hard ?coded\b",
    ),
    "weak_password_hash": (
        r"\bmd5\b", r"\bsha ?1\b", r"\bweak (?:password )?hash\w*\b", r"\bunsalted\b",
        r"\b(?:bcrypt|argon2|scrypt|pbkdf2)\b",
    ),
    "insecure_deserialization": (r"\bpickle\b", r"\bunpickl\w*\b", r"\bdeserializ\w*\b"),
}
# Classes that are NOT in the sample (it returns JSON, has no cookies, parses no XML, makes no outbound request and no
# redirect). Naming more than E7_MAX_EXTRA_CLAIMS of them is a shotgun answer that never read the code.
E7_ABSENT: dict[str, tuple[str, ...]] = {
    "xss": (r"\bxss\b", r"\bcross site scripting\b"),
    "csrf": (r"\bcsrf\b", r"\bcross site request forgery\b"),
    "xxe": (r"\bxxe\b", r"\bxml external entit\w*\b"),
    "ssrf": (r"\bssrf\b", r"\bserver side request forgery\b"),
    "open_redirect": (r"\bopen redirect\w*\b",),
}
E7_MIN_FOUND = 5         # of the 6 seeded
E7_MAX_EXTRA_CLAIMS = 2  # absent classes an answer may name before it counts as a shotgun


def _e7_fixture(workdir: pathlib.Path) -> dict:
    return {"code": E7_CODE}


def _e7_prompt(fixture: dict) -> str:
    return (
        _PREAMBLE
        + "You are a security reviewer. Review this code (a sample for an evaluation; it is not run):\n\n```python\n"
        + fixture["code"]
        + "```\n\nList every security vulnerability that is actually present, one per line, as: "
        "`- <function or line>: <vulnerability class> - <how it can be exploited> - <fix>`. Report only "
        "vulnerabilities that exist in this code: do not list classes of problems that are not there."
    )


def _e7_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    found = text.topic_hits(output, E7_SEEDED)
    absent = text.topic_hits(output, E7_ABSENT)
    named = sum(found.values())
    extra = sum(absent.values())
    findings: dict = {"seeded_named": named, "seeded_total": len(E7_SEEDED), "seeded_needed": E7_MIN_FOUND,
                      "absent_classes_claimed": extra, "absent_claims_allowed": E7_MAX_EXTRA_CLAIMS}
    findings.update({f"named_{name}": hit for name, hit in found.items()})
    return Score(
        success=named >= E7_MIN_FOUND and extra <= E7_MAX_EXTRA_CLAIMS,
        findings=findings,
        notes="keyword heuristic over normalised text; naming classes that are not in the code counts against it",
    )


# ---- E9 Tool use: a JSON tool call with at most one corrected retry -----------------------------------------------

# name -> {summary, params: {name: rule}}. A rule has type ('string', 'number' or 'integer'), required, and optionally
# enum, min and max, pattern and note. The same table renders the prompt and validates the call.
E9_TOOLS: dict[str, dict] = {
    "get_weather": {
        "summary": "Forecast for a city.",
        "params": {
            "city": {"type": "string", "required": True},
            "unit": {"type": "string", "required": True, "enum": ("celsius", "fahrenheit")},
        },
    },
    "create_ticket": {
        "summary": "Open a ticket in the tracker.",
        "params": {
            "title": {"type": "string", "required": True},
            "priority": {"type": "string", "required": True, "enum": ("low", "medium", "high")},
            "assignee": {"type": "string", "required": False},
        },
    },
    "search_docs": {
        "summary": "Search the documentation.",
        "params": {
            "query": {"type": "string", "required": True},
            "limit": {"type": "integer", "required": False, "min": 1, "max": 20},
        },
    },
    "convert_currency": {
        "summary": "Convert an amount between two currencies.",
        "params": {
            "amount": {"type": "number", "required": True},
            "from_currency": {"type": "string", "required": True, "pattern": r"[A-Z]{3}",
                              "note": "3-letter upper case ISO code"},
            "to_currency": {"type": "string", "required": True, "pattern": r"[A-Z]{3}",
                            "note": "3-letter upper case ISO code"},
        },
    },
}
E9_REQUEST = 'File an urgent ticket titled "Payment page returns 500" and assign it to Maria.'
E9_EXPECTED = {"tool": "create_ticket", "title": "Payment page returns 500", "priority": "high", "assignee": "maria"}


def _render_e9_tools() -> str:
    lines = []
    for number, (name, spec) in enumerate(E9_TOOLS.items(), start=1):
        lines.append(f"{number}. {name}: {spec['summary']}")
        parts = []
        for pname, rule in spec["params"].items():
            bits = [rule["type"], "required" if rule.get("required") else "optional"]
            if "enum" in rule:
                bits.append("one of: " + ", ".join(rule["enum"]))
            if "min" in rule:
                bits.append(f"{rule['min']} to {rule['max']}")
            if "note" in rule:
                bits.append(rule["note"])
            parts.append(f"{pname} ({', '.join(bits)})")
        lines.append("   arguments: " + "; ".join(parts))
    return "\n".join(lines)


def _check_value(name: str, value: object, rule: dict) -> list[str]:
    kind = rule["type"]
    if kind == "string":
        if not isinstance(value, str) or not value.strip():
            return [f"argument {name!r} must be a non-empty string (got {value!r})"]
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"argument {name!r} must be a number (got {value!r})"]
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"argument {name!r} must be an integer (got {value!r})"]
    if "enum" in rule and value not in rule["enum"]:
        return [f"argument {name!r} must be one of {', '.join(rule['enum'])} (got {value!r})"]
    if "min" in rule and not rule["min"] <= value <= rule["max"]:
        return [f"argument {name!r} must be between {rule['min']} and {rule['max']} (got {value!r})"]
    if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
        return [f"argument {name!r} must be a {rule.get('note', 'valid value')} (got {value!r})"]
    return []


def check_tool_call(call: object) -> list[str]:
    """What the fake tool layer would answer to `call`: a list of error messages, empty when the call is well formed
    (a known tool, only known arguments, every required one present, right types and values). Whether the call does
    what the user asked is a separate question, answered by the scorer."""
    if not isinstance(call, dict):
        return ["the reply must be a JSON object"]
    errors: list[str] = []
    for field in ("tool", "arguments"):
        if field not in call:
            errors.append(f'the object needs a "{field}" field')
    extra = sorted(set(call) - {"tool", "arguments"})
    if extra:
        errors.append("unexpected field(s): " + ", ".join(extra))
    tool = call.get("tool")
    if "tool" in call and (not isinstance(tool, str) or tool not in E9_TOOLS):
        errors.append(f"unknown tool {tool!r}; the tools are " + ", ".join(E9_TOOLS))
        return errors
    if "arguments" not in call:
        return errors
    arguments = call["arguments"]
    if not isinstance(arguments, dict):
        errors.append('"arguments" must be a JSON object')
        return errors
    if tool is None:
        return errors
    params = E9_TOOLS[tool]["params"]
    for name in arguments:
        if name not in params:
            errors.append(f"unknown argument {name!r} for {tool}")
    for name, rule in params.items():
        if name not in arguments:
            if rule.get("required"):
                errors.append(f"missing required argument {name!r}")
            continue
        errors.extend(_check_value(name, arguments[name], rule))
    return errors


def _parse_e9_attempt(reply: str) -> tuple[object, list[str]]:
    """(the call object or None, the fake tool's error messages) for one reply."""
    objects = text.extract_json_objects(reply)
    if not objects:
        return None, ["no JSON object was found in the reply"]
    errors = check_tool_call(objects[0])
    if len(objects) > 1:
        errors.append("the reply holds more than one JSON object: send exactly one tool call")
    return objects[0], errors


def _e9_matches_request(call: dict) -> bool:
    arguments = call.get("arguments") or {}
    return (
        call.get("tool") == E9_EXPECTED["tool"]
        and str(arguments.get("title", "")).strip() == E9_EXPECTED["title"]
        and arguments.get("priority") == E9_EXPECTED["priority"]
        and str(arguments.get("assignee", "")).strip().lower() == E9_EXPECTED["assignee"]
    )


def _e9_fixture(workdir: pathlib.Path) -> dict:
    return {"request": E9_REQUEST}


def _e9_prompt(fixture: dict) -> str:
    return (
        _PREAMBLE
        + "You can call the tools below. They are fake tools for a test: you cannot see their code and there is "
        "nothing to run.\n\n" + _render_e9_tools() + "\n\nTo call a tool, reply with exactly one JSON object and "
        'nothing else: {"tool": "<tool name>", "arguments": {"<argument name>": <value>, ...}}\n\n'
        "User request: " + fixture["request"]
    )


def _e9_retry_prompt(fixture: dict, last_output: str) -> str | None:
    """The corrected-retry prompt, when the last reply is not a well formed call: the fake tool's own error messages,
    the way a real tool layer reports a bad call. None when the reply is well formed (right or wrong): a call the tool
    accepts is not retried, it is scored."""
    _, errors = _parse_e9_attempt(last_output)
    if not errors:
        return None
    return (
        _e9_prompt(fixture)
        + "\n\nYour previous reply was:\n" + text.clip(last_output.strip(), 1500)
        + "\n\nThe tool rejected it and nothing was done. It returned:\n"
        + "\n".join("error: " + e for e in errors)
        + "\n\nReply with one corrected JSON tool call and nothing else."
    )


def _e9_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    attempts = list(result.attempts) or [output]
    parsed = [_parse_e9_attempt(reply) for reply in attempts]
    retries = len(attempts) - 1
    first_call, first_errors = parsed[0]
    final_call, final_errors = parsed[-1]
    first_valid = not first_errors
    final_valid = not final_errors
    first_correct = first_valid and _e9_matches_request(first_call)
    final_correct = final_valid and _e9_matches_request(final_call)
    within_retry_limit = retries <= 1
    notes = "; ".join(final_errors) if final_errors else ("well formed" if final_correct else "well formed but not what "
                                                                                              "the user asked for")
    if not within_retry_limit:
        notes = f"{retries} retries: only one corrected retry is allowed"
    return Score(
        success=final_correct and within_retry_limit,
        findings={
            "first_try_valid": first_valid, "first_try_correct": first_correct, "retries_used": retries,
            "final_valid": final_valid, "final_correct": final_correct,
            "strict_json_only": text.is_bare_json(attempts[0]),
        },
        notes=notes,
    )


# ---- E10 Review: find an intentionally seeded bug in a diff ---------------------------------------------------

E10_COMMIT_MESSAGE = (
    "Add bulk discounts to the cart: 10 percent off from 10 units, 20 percent off from 50 units. Reject a quantity "
    "below 1."
)

_E10_PRICING_BEFORE = '''"""Price arithmetic for the shop."""


def line_total(quantity, unit_price):
    """Price of one order line before any discount."""
    return round(quantity * unit_price, 2)
'''

# THE SEEDED BUG: the tiers are documented as 'from 10 units' and 'from 50 units' but the comparisons are strict, so a
# line of exactly 10 or exactly 50 units gets no discount. The added tests only try 5 and 100 units.
_E10_PRICING_AFTER = '''"""Price arithmetic for the shop."""


def line_total(quantity, unit_price):
    """Price of one order line before any discount."""
    return round(quantity * unit_price, 2)


def bulk_discount(quantity, unit_price):
    """Price of one order line after the bulk discount.

    10 percent off from 10 units, 20 percent off from 50 units.
    """
    if quantity > 50:
        rate = 0.20
    elif quantity > 10:
        rate = 0.10
    else:
        rate = 0.0
    return round(quantity * unit_price * (1 - rate), 2)
'''

_E10_CART_BEFORE = '''"""A shopping cart."""
from .pricing import line_total


class Cart:
    def __init__(self):
        self.lines = []

    def add(self, sku, quantity, unit_price):
        self.lines.append((sku, quantity, unit_price))

    def total(self):
        return round(sum(line_total(q, p) for _, q, p in self.lines), 2)
'''

_E10_CART_AFTER = '''"""A shopping cart."""
from .pricing import bulk_discount


class Cart:
    def __init__(self):
        self.lines = []

    def add(self, sku, quantity, unit_price):
        if quantity < 1:
            raise ValueError("quantity must be at least 1")
        self.lines.append((sku, quantity, unit_price))

    def total(self):
        return round(sum(bulk_discount(q, p) for _, q, p in self.lines), 2)
'''

_E10_TEST_AFTER = '''from shop.pricing import bulk_discount


def test_no_discount_for_small_orders():
    assert bulk_discount(5, 2.0) == 10.0


def test_large_orders_get_the_top_discount():
    assert bulk_discount(100, 1.0) == 80.0
'''

E10_BUGGY_FILE = "shop/pricing.py"
E10_FILES: dict[str, tuple[str, str]] = {
    "shop/pricing.py": (_E10_PRICING_BEFORE, _E10_PRICING_AFTER),
    "shop/cart.py": (_E10_CART_BEFORE, _E10_CART_AFTER),
    "tests/test_pricing.py": ("", _E10_TEST_AFTER),
}
_E10_FILE_PATTERN = r"(?<![a-z0-9_])(?:shop/)?pricing\.py(?![a-z0-9_])"
# The defect class of the seeded bug: a boundary or off-by-one comparison (the operator is on the wrong side of it).
_E10_DEFECT_PATTERNS = (
    r"off[- ]by[- ](?:one|1)", r"fence[- ]?post", r"boundar(?:y|ies)", r">=", r"greater than or equal", r"\binclusive\b",
    r"\bexclusive\b", r"exactly (?:10|50|ten|fifty)", r"edge case", r"strictly (?:greater|more)",
    r"comparison operator",
)
_E10_STATUS = re.compile(r"review_status[\"'`*]*\s*[:=]\s*[*`\"']*\s*(pass|changes_required|blocked)")


def build_e10_diff() -> str:
    """The diff under review, built with difflib from the before and after texts so it is a valid unified diff."""
    parts = []
    for path, (before, after) in E10_FILES.items():
        old = "/dev/null" if not before else f"a/{path}"
        parts.append(f"diff --git a/{path} b/{path}\n")
        parts.append("".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=old, tofile=f"b/{path}",
        )))
    return "".join(parts)


def _e10_fixture(workdir: pathlib.Path) -> dict:
    return {"diff": build_e10_diff(), "commit_message": E10_COMMIT_MESSAGE}


def _e10_prompt(fixture: dict) -> str:
    return (
        _PREAMBLE
        + "You are the independent reviewer of a change. Review it as you would before allowing a merge.\n\n"
        "Commit message: " + fixture["commit_message"] + "\n\n```diff\n" + fixture["diff"] + "```\n\n"
        "Reply in this form:\n"
        "review_status: PASS or CHANGES_REQUIRED\n"
        "findings:\n"
        "- file: <path> | defect: <the class of defect, for example off-by-one, injection, race condition, resource "
        "leak, wrong operator, missing validation> | why: <one or two sentences>\n"
        "Report only real defects, one line each. A change with no real defect gets PASS and no findings."
    )


def _e10_score(fixture: dict, workdir: pathlib.Path, output: str, result: InvokeResult) -> Score:
    low = output.lower()
    statuses = _E10_STATUS.findall(low)
    approved = bool(statuses) and statuses[-1] == "pass"
    file_named = bool(re.search(_E10_FILE_PATTERN, low))
    defect_named = text.matches_any(low, _E10_DEFECT_PATTERNS)
    together = any(
        re.search(_E10_FILE_PATTERN, block.lower()) and text.matches_any(block, _E10_DEFECT_PATTERNS)
        for block in text.split_blocks(output)
    )
    return Score(
        success=bool(together) and not approved,
        findings={"file_named": file_named, "defect_named": defect_named, "file_and_defect_together": bool(together),
                  "approved": approved},
        notes="the file and the defect class must sit in one finding, and the review must not approve the change",
    )


# ---- the task objects ---------------------------------------------------------------------------------------------

# What one `hermes -z` call costs against a provider's daily quota when the model answers in one turn: the main call and
# Hermes's auxiliary title-generation call. Measured on 2026-09-18 in the Phase 2 usage files (a plain text one-shot: api_calls
# 1, auxiliary title_generation api_calls 1, total_including_auxiliary api_calls 2), and Hermes's own oneshot.py says
# pipelines bill on that grand total. A task that allows a retry pays it once per call, because every call is its own session.
CALL_REQUESTS = 2

E1 = EvalTask(
    id="E1", title="Requirements", kind=KIND_TEXT, build_fixture=_e1_fixture, build_prompt=_e1_prompt,
    score=_e1_score, est_requests=CALL_REQUESTS,
    what="turn a vague requirement into explicit assumptions",
)
E2 = EvalTask(
    id="E2", title="Architecture", kind=KIND_TEXT, build_fixture=_e2_fixture, build_prompt=_e2_prompt,
    score=_e2_score, est_requests=CALL_REQUESTS,
    what="define clean component boundaries and interfaces",
)
# An estimate, not a measurement: reading a seven file repository to answer six questions is about a dozen tool turns.
E3 = EvalTask(
    id="E3", title="Repository understanding", kind=KIND_REPO, build_fixture=_e3_fixture, build_prompt=_e3_prompt,
    score=_e3_score, est_requests=12 + 1, tools=("file",), timeout_seconds=1200,
    what="inspect a small repository with file tools and answer questions about it",
)
E7 = EvalTask(
    id="E7", title="Security", kind=KIND_TEXT, build_fixture=_e7_fixture, build_prompt=_e7_prompt,
    score=_e7_score, est_requests=CALL_REQUESTS,
    what="name the concrete vulnerabilities seeded in a code sample",
)
E9 = EvalTask(
    id="E9", title="Tool use", kind=KIND_TEXT, build_fixture=_e9_fixture, build_prompt=_e9_prompt,
    score=_e9_score, est_requests=2 * CALL_REQUESTS, max_retries=1, retry_prompt=_e9_retry_prompt,
    what="emit a valid JSON tool call and recover from one error",
)
E10 = EvalTask(
    id="E10", title="Review", kind=KIND_TEXT, build_fixture=_e10_fixture, build_prompt=_e10_prompt,
    score=_e10_score, est_requests=CALL_REQUESTS,
    what="find the intentionally seeded bug in a diff and name its file and defect class",
)
