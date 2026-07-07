"""Shared logic for the live Google-reviews feed. Stdlib-only (no pip) so it runs
on the GitHub Actions runner.

Design: we DO NOT change the site's format. Two existing components display
reviews and we only swap their *content* with real Google reviews:
  1. The narrow top ribbon  -> view 3497 `website.sd_nav_testimonial`
     (custom CSS + JS rotator + server-rendered `.sd-nq` spans). Kept unbroken.
  2. (optional) the larger testimonial carousel further down -> handled by a
     separate one-time script, not daily.

Used by cloud-cron/fetch_google_reviews.py (daily) and the one-time installer.
"""
import html
import json
import re
import urllib.request
import urllib.error
from datetime import datetime

_DETAIL_MASK = (
    "id,displayName,rating,userRatingCount,googleMapsUri,"
    "reviews.rating,reviews.text,reviews.originalText,"
    "reviews.authorAttribution,reviews.publishTime,"
    "reviews.relativePublishTimeDescription"
)


def fetch_reviews(api_key: str, place_id: str) -> dict:
    """Return {rating, count, maps_uri, reviews:[{rating,text,author,date,when}]}."""
    req = urllib.request.Request(
        f"https://places.googleapis.com/v1/places/{place_id}",
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _DETAIL_MASK},
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=25).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Places API HTTP {e.code}: {e.read().decode()[:400]}") from e

    reviews = []
    for r in data.get("reviews", []):
        text = (r.get("text") or r.get("originalText") or {}).get("text", "").strip()
        if not text:
            continue
        date = ""
        pt = r.get("publishTime", "")
        if pt:
            try:
                date = datetime.fromisoformat(pt.replace("Z", "+00:00")).strftime("%b %Y")
            except ValueError:
                date = ""
        reviews.append({
            "rating": int(r.get("rating", 5)),
            "text": text,
            "author": (r.get("authorAttribution") or {}).get("displayName", "Google user"),
            "date": date,
            "when": r.get("relativePublishTimeDescription", ""),
        })
    return {
        "rating": data.get("rating"),
        "count": data.get("userRatingCount", 0),
        "maps_uri": data.get("googleMapsUri", ""),
        "reviews": reviews,
    }


# ---------- text helpers ----------

def short_snippet(text: str, limit: int = 90) -> str:
    """One-line ribbon quote: show the WHOLE review if it fits the line (~90 chars),
    otherwise trim at a word boundary near the cap with an ellipsis. No longer
    forces first-sentence-only, so short reviews show in full."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:. ") + "…"


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _clean_author(s: str, limit: int = 40) -> str:
    """Collapse whitespace and length-cap author names before they hit markup."""
    s = " ".join((s or "").split())
    return (s[:limit].rstrip() + "…") if len(s) > limit else s


def _js_str(s: str) -> str:
    """Safely embed an arbitrary string as a JS string literal inside a <script>.
    json.dumps handles quotes/backslashes/control chars; the extra replacements
    stop a review containing `</script>` or the JS line separators U+2028/U+2029
    from breaking out of the script block (stored-XSS via public Google reviews)."""
    return (json.dumps(s, ensure_ascii=False)
            .replace("</", "<\\/")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def clean_over_escaping(s: str) -> str:
    """Collapse the &amp;amp; accumulation that arch_db round-trips create (Rule 6b)."""
    prev = None
    while s != prev:
        prev = s
        s = s.replace("&amp;amp;", "&amp;")
    return s.replace("&amp;gt;", "&gt;").replace("&amp;lt;", "&lt;")


# ---------- ribbon (view 3497) rendering ----------

def ribbon_spans(reviews: list, max_items: int = 5) -> str:
    """Rebuild the visible `.sd-nq` spans (first = active). Same markup as installed."""
    out = []
    for i, r in enumerate(reviews[:max_items]):
        who = _esc(_clean_author(r["author"]))
        if r["date"]:
            who += ", " + _esc(r["date"])
        active = " active" if i == 0 else ""
        out.append(
            f'<span class="sd-nq{active}"><span class="sd-stars">★★★★★</span>'
            f'<em>"{_esc(short_snippet(r["text"]))}"</em>'
            f'<strong> — {who}</strong></span>'
        )
    return "".join(out)


def ribbon_js_array(reviews: list, max_items: int = 5) -> str:
    """Rebuild the JS `quotes = [...]` array (kept in sync even though it's a fallback)."""
    items = []
    for r in reviews[:max_items]:
        t = _js_str(short_snippet(r["text"]))
        a = _js_str(_clean_author(r["author"]))
        d = _js_str(r["date"])
        items.append("        {text: %s, author: %s, date: %s}" % (t, a, d))
    return "var quotes = [\n" + ",\n".join(items) + "\n    ];"


_BAR_RE = re.compile(r'(<div class="sd-testimonial-bar">)(.*?)(</div>)', re.DOTALL)
_JS_RE = re.compile(r"var quotes = \[.*?\];", re.DOTALL)
# the rotator's setInterval — uniquely the one immediately before `return true;`
_ROT_RE = re.compile(r"(\}, )(\d+)(\);\s*return true;)")


# ---------- larger carousel (view 585 #reviews s_quotes) rendering ----------

SL_START, SL_END = "<!--GR_SLIDES_START-->", "<!--GR_SLIDES_END-->"
DOT_START, DOT_END = "<!--GR_DOTS_START-->", "<!--GR_DOTS_END-->"

_BLOCKQUOTE_CLS = ("s_blockquote s_blockquote_with_icon o_cc o_animable position-relative "
                   "d-flex flex-column gap-4 w-50 mx-auto p-5 fst-normal o_cc2 o_colored_level")


def carousel_snippet(text: str, limit: int = 650) -> str:
    """Full-ish review text for a testimonial slide; generous cap so most reviews
    show in full, only very long ones get trimmed."""
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def carousel_slides(reviews: list, max_items: int = 6) -> str:
    """Rebuild the .carousel-item slides (first = active). Odoo s_blockquote markup,
    demo avatar swapped for a star row (Google author photos are external / blocked)."""
    out = []
    for i, r in enumerate(reviews[:max_items]):
        active = " active" if i == 0 else ""
        who = _esc(_clean_author(r["author"]))
        prov = f'Google review · {_esc(r["date"])}' if r["date"] else "Google review"
        date = f'<br/><span class="text-muted">{prov}</span>'
        stars = "★" * max(1, min(5, r["rating"]))
        out.append(
            f'<div class="carousel-item{active} oe_img_bg o_bg_img_center pb80 pt168 o_colored_level" data-name="Slide">'
            f'<blockquote data-name="Blockquote" data-snippet="s_blockquote" class="{_BLOCKQUOTE_CLS}" data-vcss="001">'
            '<div class="s_blockquote_line_elt position-absolute top-0 start-0 bottom-0 bg-o-color-1"/>'
            '<div class="s_blockquote_wrap_icon position-absolute top-0 start-50 translate-middle w-100">'
            '<i class="s_blockquote_icon fa fa-quote-right d-block mx-auto rounded bg-o-color-1" role="img"/>'
            '</div>'
            f'<p class="s_blockquote_quote my-auto h4-fs" style="text-align:center;">"{_esc(carousel_snippet(r["text"]))}"</p>'
            '<div class="s_blockquote_infos d-flex gap-2 flex-column align-items-center w-100 text-center">'
            f'<div style="color:#F59E0B;letter-spacing:2px;font-size:1.15rem;line-height:1;">{stars}</div>'
            f'<div class="s_blockquote_author"><span class="o_small-fs"><strong> {who} </strong>{date}</span></div>'
            '</div></blockquote></div>'
        )
    return SL_START + "".join(out) + SL_END


def carousel_dots(reviews, carousel_id: str, max_items: int = 6) -> str:
    """Rebuild the carousel-indicators buttons to match the slide count."""
    n = min(len(reviews), max_items)
    out = []
    for i in range(n):
        cls = ' class="active"' if i == 0 else ""
        cur = ' aria-current="true"' if i == 0 else ""
        out.append(
            f'<button type="button"{cls} aria-label="Carousel indicator" '
            f'data-bs-target="#{carousel_id}" data-bs-slide-to="{i}"{cur}/>'
        )
    return DOT_START + "".join(out) + DOT_END


_SLIDES_RE = re.compile(re.escape(SL_START) + r".*?" + re.escape(SL_END), re.DOTALL)
_DOTS_RE = re.compile(re.escape(DOT_START) + r".*?" + re.escape(DOT_END), re.DOTALL)
_INTERVAL_RE = re.compile(r'(s_quotes_carousel carousel[^>]*?data-bs-interval=")\d+(")')
_RIDE_RE = re.compile(r'(s_quotes_carousel carousel[^>]*?data-bs-ride=")[^"]*(")')
_CID_RE = re.compile(r'id="(myQuoteCarousel\d+)"')


def update_carousel_arch(arch: str, reviews: list, interval: int = 10000) -> str:
    """Refresh the #reviews carousel slides + dots between markers, set the cycle
    interval, and ensure it AUTOPLAYS (data-bs-ride='carousel' — 'true' only cycles
    after a manual swipe). Requires the one-time install to have added the markers."""
    m = _CID_RE.search(arch)
    if not m:
        raise RuntimeError("carousel id not found")
    cid = m.group(1)
    if SL_START not in arch or DOT_START not in arch:
        raise RuntimeError("GR carousel markers not found — run _push_reviews_carousel.py first")
    new = _INTERVAL_RE.sub(lambda mm: mm.group(1) + str(interval) + mm.group(2), arch, count=1)
    new = _RIDE_RE.sub(lambda mm: mm.group(1) + "carousel" + mm.group(2), new, count=1)
    new = _SLIDES_RE.sub(lambda _mm: carousel_slides(reviews), new, count=1)
    new = _DOTS_RE.sub(lambda _mm: carousel_dots(reviews, cid), new, count=1)
    return clean_over_escaping(new)


def update_ribbon_arch(arch: str, reviews: list, interval_ms: int = 5000) -> str:
    """Swap the ribbon's visible spans + JS array with fresh reviews, and set the
    rotator cycle to interval_ms. Guarded."""
    new = arch
    n_bar = len(_BAR_RE.findall(new))
    if n_bar != 1:
        raise RuntimeError(f"expected 1 .sd-testimonial-bar div, found {n_bar}")
    new = _BAR_RE.sub(lambda m: m.group(1) + ribbon_spans(reviews) + m.group(3), new, count=1)

    n_js = len(_JS_RE.findall(new))
    if n_js != 1:
        raise RuntimeError(f"expected 1 'var quotes = [...]' array, found {n_js}")
    new = _JS_RE.sub(lambda m: ribbon_js_array(reviews), new, count=1)

    n_rot = len(_ROT_RE.findall(new))
    if n_rot != 1:
        raise RuntimeError(f"expected 1 rotator setInterval, found {n_rot}")
    new = _ROT_RE.sub(lambda m: m.group(1) + str(interval_ms) + m.group(3), new, count=1)
    return clean_over_escaping(new)
