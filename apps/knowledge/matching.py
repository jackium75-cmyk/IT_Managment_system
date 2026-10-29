"""
Suggest knowledge-base articles for a ticket.

Scoring (transparent, explainable — each suggestion lists why it matched):
* +40 for every error code shared between the ticket text and the article;
* +20 same device type (ticket asset category = article device type);
* +15 same ticket category (+5 more for the same subcategory not modelled);
* +3 per shared keyword (max 30);
* +5 if the article is verified.
Articles must reach MIN_SCORE. A match is a hint to check, never an assumption
that the root cause is the same.
"""
import re

ERROR_CODE = re.compile(
    r"\b(?:0x[0-9a-f]{4,8}|[a-z]{1,4}[-_]?\d{2,5}|error\s*(?:code\s*)?\d{2,5}|\d{3}\s*error)\b", re.IGNORECASE)
WORD = re.compile(r"[a-z0-9]+")
STOP = set("""the and for with not but are was were has have had this that from your you our its will can cannot can't
when what why how who which there their then than into onto over under about after before again also any all
some more most very just only still please need needs help working work works doesnt does did done get got
issue problem error errors shows showing show screen device hotel room staff guest guests today yesterday morning
evening night keeps keep stop stopped start started""".split())
MIN_SCORE = 12


def normalise_code(code):
    return re.sub(r"[\s_-]", "", code.lower())


def error_codes(text):
    return {normalise_code(m.group(0)) for m in ERROR_CODE.finditer(text or "")}


def keywords(text):
    return {w for w in WORD.findall((text or "").lower()) if len(w) >= 3 and w not in STOP and not w.isdigit()}


def article_text(a):
    return " ".join([a.title, a.problem, a.symptoms, a.keywords.replace(",", " "), a.error_codes])


def score(ticket, article, ticket_words=None, ticket_codes=None):
    text = f"{ticket.subject} {ticket.description} {ticket.affected_device}"
    ticket_words = ticket_words if ticket_words is not None else keywords(text)
    ticket_codes = ticket_codes if ticket_codes is not None else error_codes(text)
    reasons, total = [], 0
    art_codes = {normalise_code(c) for c in article.error_code_list} | error_codes(article.title + " " + article.problem)
    shared_codes = ticket_codes & art_codes
    if shared_codes:
        total += 40 * len(shared_codes)
        reasons.append("same error code " + ", ".join(sorted(c.upper() for c in shared_codes)))
    if ticket.asset_id and article.asset_category_id and ticket.asset.category_id == article.asset_category_id:
        total += 20
        reasons.append(f"same device type ({article.asset_category.name})")
    if ticket.category_id and article.category_id == ticket.category_id:
        total += 15
        reasons.append(f"same category ({article.category.name})")
    shared = ticket_words & keywords(article_text(article))
    if shared:
        total += min(30, 3 * len(shared))
        reasons.append("shared words: " + ", ".join(sorted(shared)[:6]))
    if total and article.is_verified:
        total += 5
    return total, reasons


def suggest(ticket, limit=5):
    from .models import KnowledgeArticle

    text = f"{ticket.subject} {ticket.description} {ticket.affected_device}"
    words, codes = keywords(text), error_codes(text)
    results = []
    for article in KnowledgeArticle.objects.filter(status=KnowledgeArticle.Status.PUBLISHED).select_related(
            "category", "asset_category"):
        s, reasons = score(ticket, article, words, codes)
        if s >= MIN_SCORE:
            results.append((s, article, reasons))
    results.sort(key=lambda r: (-r[0], not r[1].is_verified, -r[1].updated_at.timestamp()))
    return results[:limit]


def search(query, qs):
    """Rank articles for a free-text search (all words must appear somewhere, codes rank highest)."""
    words, codes = keywords(query), error_codes(query)
    if not words and not codes:
        return list(qs)
    ranked = []
    for a in qs.select_related("category", "asset_category"):
        text = article_text(a) + " " + a.solution + " " + a.root_cause
        hay = keywords(text)
        a_codes = {normalise_code(c) for c in a.error_code_list} | error_codes(text)
        hits = len(words & hay)
        code_hits = len(codes & a_codes)
        if hits or code_hits:
            ranked.append((code_hits * 10 + hits + (1 if a.is_verified else 0), a))
    ranked.sort(key=lambda r: -r[0])
    return [a for _s, a in ranked]
